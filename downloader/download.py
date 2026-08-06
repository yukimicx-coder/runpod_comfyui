import argparse
import hashlib
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import httpx
import yaml
from dotenv import load_dotenv
from littledl import DownloadConfig, DownloadError, ProgressEvent, download_file_sync
from tqdm import tqdm

load_dotenv()

from huggingface_hub import HfApi

#
# Customizable Environment variable
#
# MODEL_DL_ROOT, MODEL_DL_LOG, MODEL_DL_LIST
# HF_TOKEN
# CIVITAI_API_TOKEN, CIVITAI_API_URL
#

DL_ROOT=os.environ.get("MODEL_DL_ROOT", "/workspace")
MODEL_DL_LOG=os.environ.get("MODEL_DL_LOG", os.path.join(DL_ROOT, "dl.log"))
MODEL_DL_LIST=os.environ.get("MODEL_DL_LIST", os.path.join(DL_ROOT, "dl_list.yaml"))

CIVITAI_API_URL = os.environ.get("CIVITAI_API_URL", "https://civitai.com/api/v1")

DEFAULT_BASE_DIR="models"

# logger

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
lh = logging.StreamHandler()
lh.setFormatter(logging.Formatter('[%(levelname)s] - %(message)s'))
logger.addHandler(lh)

os.makedirs(os.path.dirname(MODEL_DL_LOG), exist_ok=True)
lh = logging.FileHandler(MODEL_DL_LOG)
lh.setFormatter(logging.Formatter('[%(levelname)s] - (%(asctime)s) - %(message)s'))
logger.addHandler(lh)


def compare_hashes(local_path, hash_hex:str):
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b''):
            h.update(chunk)
    this_hash = h.hexdigest().lower()
    orig_hash = hash_hex.lower()
    #logger.debug(f"orig={orig_hash}, this={this_hash}")
    return this_hash == orig_hash

def hf_get_path_info(repo_id, filename, subfolder=None):
    hf_api = HfApi()
    file_path = filename
    if subfolder:
        file_path = os.path.join(subfolder, file_path)
    return hf_api.get_paths_info(repo_id=repo_id, paths=[file_path])[0]

def _update_file_state(model_inf:dict, remote_hash:str):
    if os.path.isfile(model_inf["saved_as"]):
        if compare_hashes(model_inf["saved_as"], remote_hash):
            model_inf["state"] = "same"
        else:
            model_inf["state"] = "different"
    else:
        model_inf["state"] = "missing"

    if model_inf["state"] == "same" and not model_inf.get("overwrite", False):
        model_inf["result"] = "info: skip same file"

def _download_file(url: str, headers: dict, saved_as: str,
                   expected_sha256: str | None = None,
                   expected_size_bytes: int | None = None,
                   label: str = "",
                   max_connections: int = 1,
                   segment_threshold_bytes: int = 0) -> str:

    use_segmented = (max_connections > 1 and expected_size_bytes
                     and expected_size_bytes >= segment_threshold_bytes)

    config = DownloadConfig(
        enable_chunking=use_segmented,
        max_chunks=max_connections,
        resume=True,
        verify_hash=expected_sha256 is not None,
        expected_hash=expected_sha256,
        fallback_to_single_on_failure=True,
        headers=headers,
        timeout=600,
    )

    save_dir = os.path.dirname(saved_as)
    filename = os.path.basename(saved_as)

    pbar = tqdm(total=expected_size_bytes, unit='iB', unit_scale=True,
                desc=label, leave=False, disable=args.no_progress)

    def on_progress(event: ProgressEvent):
        pbar.n = event.downloaded
        pbar.refresh()

    try:
        download_file_sync(url, save_dir, filename, config=config,
                          progress_callback=on_progress)
        return "success"
    except DownloadError:
        return "error: " + traceback.format_exc()
    finally:
        pbar.close()

def _download_direct(url: str, headers: dict, saved_as: str,
                     expected_sha256: str | None = None,
                     expected_size_bytes: int | None = None,
                     label: str = "") -> str:
    part_path = saved_as + ".part"
    hasher = hashlib.sha256()

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        try:
            with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()

                with open(part_path, "wb") as f, \
                     tqdm(total=expected_size_bytes, unit='iB', unit_scale=True,
                          desc=label, leave=False, disable=args.no_progress) as pbar:
                    for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            hasher.update(chunk)
                            pbar.update(len(chunk))

                if expected_sha256 and expected_sha256.lower() != hasher.hexdigest().lower():
                    return f"error: hash mismatch {expected_sha256} != {hasher.hexdigest()}"

                os.replace(part_path, saved_as)
                return "success"

        except httpx.HTTPStatusError:
            return "error: " + traceback.format_exc()
        except Exception:
            return "error: " + traceback.format_exc()


def download_from_hf(model_inf:dict, do_not_dl:bool):
    if model_inf.get("result"):
        if model_inf["result"] != "success":
            return model_inf
    else:
        if "/" in model_inf['file']:
            sub_dir = os.path.dirname(model_inf["file"])
            model_inf["file"] = os.path.basename(model_inf["file"])
            if model_inf.get("subdir", sub_dir) != sub_dir:
                logger.warning("conflict 'subdir' and dirname of 'file': %s != %s", model_inf["subdir"], sub_dir)
            model_inf["subdir"] = sub_dir

        local_dir = os.path.join(DL_ROOT, model_inf["base_dir"], model_inf.get("ldir", "."))
        os.makedirs(local_dir, exist_ok=True)

        model_inf["saved_as"] = os.path.join(local_dir, model_inf.get("lfile", model_inf["file"]))

        logger.info("## hf: get file info: %s", model_inf['file'])
        path_info = hf_get_path_info(model_inf["repo"], model_inf["file"], model_inf.get("subdir"))
        remote_hash = None
        if path_info.lfs:
            remote_hash = path_info.lfs["sha256"].lower()
        model_inf["sha256"] = remote_hash
        model_inf["sizeKiB"] = path_info.size/1024.0

        url_path = model_inf.get("subdir", "")
        if url_path:
            url_path += "/"
        model_inf["file_url"] = f"https://huggingface.co/{model_inf['repo']}/resolve/main/{url_path}{model_inf['file']}"

        _update_file_state(model_inf, remote_hash)
        if model_inf.get("result", "") == "info: skip same file":
            return model_inf

    if do_not_dl:
        model_inf["result"] = "success"
        return model_inf

    headers = {}
    if args.max_segments > 1:
        headers["Accept-Encoding"] = "identity"
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    logger.info("## hf start downloading: %s", model_inf['file'])
    model_inf["result"] = _download_file(
        model_inf["file_url"], headers, model_inf["saved_as"],
        expected_sha256=model_inf.get("sha256"),
        expected_size_bytes=int(model_inf["sizeKiB"] * 1024),
        label=f"hf: {model_inf['file']}",
        max_connections=args.max_segments,
        segment_threshold_bytes=args.segment_threshold * (1024**3))
    return model_inf

PARTIAL_SUFFIX=".part"

def download_from_civitai(model_inf:dict, do_not_dl:bool):
    headers = {}
    civitai_token = os.environ.get("CIVITAI_API_TOKEN")
    if civitai_token:
        headers["Authorization"] = f"Bearer {civitai_token}"

    if model_inf.get("result"):
        if model_inf["result"] != "success":
            return model_inf
    else:
        url_path = f"/model-versions/{model_inf['version-id']}"

        logger.info("## civitai: start get info: %s", url_path)

        try:
            url = CIVITAI_API_URL + url_path
            response = httpx.get(url, headers=headers)
            response.raise_for_status()
        except Exception:
            model_inf["result"] = "error: " + traceback.format_exc()
            return model_inf

        versions_data = response.json()
        file_index = 0
        if "file-index" in model_inf:
            file_index = int(model_inf["file-index"])
        elif len(versions_data["files"]) > 1:
            fp_list = {}
            for i,f in enumerate(versions_data["files"]):
                fp_list[f["metadata"]["fp"]] = i
            file_index = fp_list.get("fp8",
                            fp_list.get("int8",
                                fp_list.get("bf16",
                                    fp_list.get("fp16", 0))))


        file_info = versions_data["files"][file_index]

        model_inf["file"] = file_info['name']
        model_inf["sizeKiB"] = file_info["sizeKB"]
        model_inf["sha256"] = file_info["hashes"]["SHA256"]
        model_inf["file_url"] = file_info["downloadUrl"]
        if civitai_token:
            sep = '&' if '?' in model_inf["file_url"] else '?'
            model_inf["file_url"] += f"{sep}token={civitai_token}"

        local_dir = os.path.join(DL_ROOT, model_inf["base_dir"], model_inf.get("ldir", "."))
        os.makedirs(local_dir, exist_ok=True)

        model_inf["saved_as"] = os.path.join(local_dir, model_inf.get("lfile", model_inf["file"]))

        _update_file_state(model_inf, model_inf["sha256"])
        if model_inf.get("result", "") == "info: skip same file":
            return model_inf

    if do_not_dl:
        model_inf["result"] = "success"
        return model_inf

    logger.info("## civitai: start downloading: %s", model_inf["file"])
    model_inf["result"] = _download_direct(
        model_inf["file_url"], headers, model_inf["saved_as"],
        expected_sha256=model_inf["sha256"],
        expected_size_bytes=int(model_inf["sizeKiB"] * 1024),
        label=f"civitai: {model_inf['file']}")
    return model_inf

def download_from_url(model_inf:dict, do_not_dl:bool):
    if not do_not_dl:
        model_inf["result"] = _download_direct(
            model_inf["file_url"], None, model_inf["saved_as"]
        )
        return model_inf

    missing = []
    if not model_inf.get("url"):
        missing.append('url')
    if not model_inf.get("lfile"):
        missing.append("lfile")
    if missing:
        msg = f"missing key(s): {missing}"
        model_inf["result"] = f"error: {msg}"
        logger.warning("%s: %s", msg, model_inf)
        return model_inf

    model_inf["file"] = model_inf["lfile"]

    try:
        parsed = httpx.URL(model_inf["url"])
        if not parsed.scheme or not parsed.host:
            raise ValueError()
    except:
        msg = f"invalid url: {model_inf['url']}"
        logger.warning(msg)
        model_inf["result"] = "error: " + msg
        return model_inf

    model_inf["file_url"] = model_inf["url"]

    local_path = os.path.abspath(os.path.join(DL_ROOT, model_inf["base_dir"], model_inf.get("ldir", "."), model_inf["lfile"]))
    if not local_path.startswith(DL_ROOT):
        msg = f"invalid local path: {local_path}"
        logger.error(msg)
        model_inf["result"] = "error :" + msg
        return model_inf

    model_inf["saved_as"] = local_path
    return model_inf



MAX_WORKERS=3
def download_files(dl_list:list, do_not_dl:bool):
    dl_queue = {
        "hf": [],
        "civitai": [],
        "url": []
    }

    for entry in dl_list:
        if not do_not_dl:
            missing = []
            if not entry.get("saved_as"):
                missing.append('saved_as')
            if not entry.get("file_url"):
                missing.append("file_url")
            if missing:
                msg = f"missing key(s): {missing}"
                entry["result"] = f"error: {msg}"
                logger.warning("%s: %s", msg, entry)
                continue

        match entry["type"]:
            case "hf":
                dl_queue["hf"].append(entry)
            case "civitai":
                dl_queue["civitai"].append(entry)
            case _:
                dl_queue["url"].append(entry)


    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        def _download_loop(which):
            for entry in dl_queue[which]:
                try:
                    match which:
                        case "hf":
                            download_from_hf(entry, do_not_dl)
                        case "civitai":
                            download_from_civitai(entry, do_not_dl)
                        case _:
                            download_from_url(entry, do_not_dl)
                except Exception as e:
                    logger.error("%s: %s\n\n%s", e.__class__.__name__, e, traceback.format_exc(e))
                    return
                if entry["result"] == "success":
                    if not do_not_dl:
                        logger.info("### Finished: %s => %s", entry["file"], entry["saved_as"])
                elif entry["result"].startswith("info:"):
                    logger.info("### %s: %s", entry.get("file", ""), entry["result"])
                else:
                    logger.warning("%s: %s", entry.get("file", ""), entry["result"])

        running_dl = []
        future = executor.submit(_download_loop, "hf")
        running_dl.append(future)
        future = executor.submit(_download_loop, "civitai")
        running_dl.append(future)
        future = executor.submit(_download_loop, "url")
        running_dl.append(future)

        while len(running_dl) > 0:
            time.sleep(3)
            running_dl = [f for f in running_dl if not f.done()]
            print(running_dl)

def remove_files(files:list):
    stat = [0, 0]
    logger.info("# Removing files...")

    def _remove_one_file(path:str):
        try:
            stat[0] += os.path.getsize(path)
            stat[1] += 1
            os.remove(path)
            logger.debug("file removed: %s", path)

        except FileNotFoundError:
            return
        except Exception as e:
            logger.error("[remove_files] %s: %s: %s", path, e.__class__.__name__, e)
            return

    for entry in files:
        file_path = entry["saved_as"]
        if not os.path.isfile(file_path):
            logger.warning("missing file: %s", file_path)
            continue

        _remove_one_file(file_path)

        # remove .partial file
        part_path = file_path + PARTIAL_SUFFIX
        if not os.path.isfile(part_path):
            continue

        _remove_one_file(part_path)

    logger.info("## files has been removed: %d files, %.2f KiB (%d GiB)", stat[1], stat[0]/1024.0, stat[0]/(1024.0**3))


CHUNK_SIZE=0
args = None

def main():
    global args, CHUNK_SIZE
    parse = argparse.ArgumentParser("model downloader")
    parse.add_argument("--enable", type=lambda x: x.split(","))
    parse.add_argument("--disable", type=lambda x: x.split(","))
    parse.add_argument("--list", action="store_true")
    parse.add_argument("--dry-run", action="store_true")
    parse.add_argument("--no-progress", action="store_true")
    parse.add_argument("--chunk-size", default=64, help="chunk size (MiB)", type=float)
    parse.add_argument("--max-segments", default=2, type=int,
                       help="number of segments for large file (1 = single-stream)")
    parse.add_argument("--segment-threshold", default=1.5, type=float,
                       help="min file size (GiB) to use segmented download (0 = disabled)")
    parse.add_argument("--verbose", action="store_true")

    args = parse.parse_args()

    CHUNK_SIZE = args.chunk_size * (1024**2)
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not os.path.isfile(MODEL_DL_LIST):
        logger.error(f"DL list file is not found: {MODEL_DL_LIST}")
        sys.exit(1)

    yml = None
    with open(MODEL_DL_LIST, "r") as mlist:
        yml = yaml.safe_load(mlist)

    if not isinstance(yml, dict):
        logger.error(f"invalid file format: top level must be mapping: {MODEL_DL_LIST}")
        sys.exit(2)

    if args.list:
        list_text = "DL Groups: \n"
        for k in yml:
            list_text += "\t{} ({}, {} files)\n".format(k, "disabled" if yml[k].get("disabled", False) else "enabled", len(yml[k].get("files",[])))
        logger.info(list_text)
        sys.exit(0)
                

    dl_list = []
    # apply group settings
    for k in yml:
        group = yml[k]
        disabled = group.get("disabled", False)
        if args.enable and k in args.enable:
            disabled = False
        if args.disable and k in args.disable:
            disabled = True

        files = group.get("files")
        if not files:
            logger.warning("missing or empty files in group: %s", k)
            continue

        base_dir = group.get("base_dir", DEFAULT_BASE_DIR)
        if ".." in base_dir:
            logger.warning("'base_dir' cannot have '..'; skip group %s", k)
            continue

        for file in files:
            if disabled or file.get("disabled", False):
                file["to_be_removed"] = True
            file["base_dir"] = base_dir

        dl_list += files

    logger.info("# Fetching metadata of files first...")
    download_files(dl_list, True)

    def _format_info(l:list, msg:str):
        total_size = sum([entry.get("sizeKiB", 0) for entry in l])
        gb = int(total_size/1024/1024)
        return f"## {msg}: {len(l)} files; total {total_size:.2f} KiB ({gb} GiB)"

    remove_list = [entry for entry in dl_list if (entry.get("to_be_removed", False) and entry.get("state", "missing") != "missing")]
    logger.info(_format_info(remove_list, "files to be removed"))

    same_list = [entry for entry in dl_list if (not entry.get("to_be_removed", False) and entry.get("state", "missing") == "same")]
    logger.info(_format_info(same_list, "same files"))

    dl_list = [entry for entry in dl_list if (not entry.get("to_be_removed", False) and entry.get("state", "missing") != "same")]
    logger.info(_format_info(dl_list, "files to be downloaded"))

    if args.dry_run:
        sys.exit(0)

    remove_files(remove_list)
   

    logger.info("# Downloading actual files...")
    download_files(dl_list, False)

    dl_list = [entry for entry in dl_list if entry["result"] == "success"]

    total_size = sum(entry.get("sizeKiB", 0) for entry in dl_list)
    logger.info("## Files have been downloaded: %d files; total %.2f KiB (%dGiB)", len(dl_list), total_size, int(total_size/1024/1024))


if __name__ == "__main__":
    main()
