import argparse
import hashlib
import logging
import os
import requests
import shutil
import sys
import time
import traceback
import yaml
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()

# HF_XET_HIGH_PERFORMANCE = 1 causes many promblems, use it at your own risk
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
from huggingface_hub import HfApi, hf_hub_download

#
# Customizable Environment variable
#
# MODEL_DL_ROOT, MODEL_DL_LOG, MODEL_DL_LIST
# HF_TOKEN
# CIVITAI_API_TOKEN, CIVITAI_API_URL
#

MODEL_DL_ROOT=os.environ.get("MODEL_DL_ROOT", "/workspace/models")
MODEL_DL_LOG=os.environ.get("MODEL_DL_LOG", "/workspace/models/dl.log")
MODEL_DL_LIST=os.environ.get("MODEL_DL_LIST", "/workspace/models/dl_list.yaml")
CIVITAI_API_URL = os.environ.get("CIVITAI_API_URL", "https://civitai.com/api/v1")

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

def hf_get_path_info(kwargs):
    hf_api = HfApi()
    file_path = kwargs["filename"]
    if "subfolder" in kwargs:
        file_path = os.path.join(kwargs["subfolder"], file_path)
    return hf_api.get_paths_info(repo_id=kwargs["repo_id"], paths=[file_path])[0]

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

def download_from_hf(model_inf:dict, do_not_dl:bool):
    kwargs = {
        "repo_id": model_inf["repo"],
        "filename": model_inf['file'],
    }

    if model_inf.get("result"):
        # phase2
        if model_inf["result"] != "success":
            return model_inf
        kwargs["local_dir"] = os.path.dirname(model_inf["saved_as"])
        if model_inf.get("subdir"):
            kwargs["subfolder"] = model_inf["subdir"]
    else:
        if "/" in model_inf['file']:
            sub_dir = os.path.dirname(model_inf["file"])
            model_inf["file"] = os.path.basename(model_inf["file"])
            if model_inf.get("subdir", sub_dir) != sub_dir:
                logger.warning("conflict 'subdir' and dirname of 'file': %s != %s", model_inf["subdir"], sub_dir)
            model_inf["subdir"] = sub_dir

        local_dir = os.path.join(MODEL_DL_ROOT, model_inf["ldir"])
        os.makedirs(local_dir, exist_ok=True)
        kwargs["local_dir"] = local_dir

        if model_inf.get("subdir"):
            kwargs["subfolder"] = model_inf["subdir"]

        model_inf["saved_as"] = os.path.join(local_dir, model_inf.get("lfile", model_inf["file"]))

        logger.info("## hf: get file info: %s", kwargs["filename"])
        path_info = hf_get_path_info(kwargs)
        remote_hash = None
        if path_info.lfs:
            remote_hash = path_info.lfs["sha256"].lower()
        model_inf["sizeKiB"] = path_info.size/1024.0

        _update_file_state(model_inf, remote_hash)
        if model_inf.get("result", "") == "info: skip same file":
            return model_inf

    if do_not_dl:
        model_inf["result"] = "success"
        return model_inf

    kwargs["local_dir_use_symlinks"] = False

    logger.info("## hf start downloading: %s", model_inf['file'])
    try:
        # 個別ファイルをダウンロード
        dl_path = hf_hub_download(**kwargs)

        if model_inf["saved_as"] != dl_path:
            shutil.move(dl_path, model_inf["saved_as"])

        model_inf["result"] = "success"
        return model_inf

    except Exception as e:
        model_inf["result"] = "error: " + traceback.format_exc()
        return model_inf

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRY=3
CHUNK_SIZE=100*1024*1024
PARTIAL_SUFFIX=".partial"

def download_from_civitai(model_inf:dict, do_not_dl:bool):
    headers = {"Authorization": f"Bearer {os.environ.get('CIVITAI_API_TOKEN', '')}"}

    if model_inf.get("result"):
        # phase2
        if model_inf["result"] != "success":
            return model_inf
    else:
        url_path = f"/model-versions/{model_inf['version-id']}"

        logger.info("## civitai: start get info: %s", url_path)

        try:
            url = CIVITAI_API_URL + url_path
            response = requests.get(url, headers=headers)
            response.raise_for_status()
        except Exception as e:
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
        model_inf["SHA256"] = file_info["hashes"]["SHA256"]
        model_inf["file_url"] = file_info["downloadUrl"]

        local_dir = os.path.join(MODEL_DL_ROOT, model_inf["ldir"])
        os.makedirs(local_dir, exist_ok=True)

        model_inf["saved_as"] = os.path.join(local_dir, model_inf.get("lfile", model_inf["file"]))

        _update_file_state(model_inf, model_inf["SHA256"])
        if model_inf.get("result", "") == "info: skip same file":
            return model_inf

    if do_not_dl:
        model_inf["result"] = "success"
        return model_inf

    partial_path = model_inf["saved_as"] + PARTIAL_SUFFIX

    sha256_hash = hashlib.sha256()
    if os.path.isfile(partial_path):
        # 中断ファイルがあればハッシュに含める
        with open(partial_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                sha256_hash.update(chunk)

    session = requests.Session()

    logger.info("## civitai: start downloading: %s", model_inf["file"])
    for retry_count in range(MAX_RETRY):
        part_len = 0
        if os.path.isfile(partial_path):
            part_len = os.path.getsize(partial_path)
            headers["Range"] = f'bytes={part_len}-'
            logger.debug("civitai: request resuming: %s", model_inf["file"])

        # ファイル本体のダウンロード
        with session.get(model_inf["file_url"], headers=headers, stream=True, timeout=30) as response:
            try:
                response.raise_for_status()
                
                if response.status_code == 206:
                    logger.debug("civitai: resuming")
                    mode = 'ab'
                else:
                    if part_len > 0:
                        # サーバーがレジュームを拒否して200を返してきた場合はハッシュをリセットして上書き
                        logger.debug("civitai: resuming was rejected. restart from zero")
                        sha256_hash = hashlib.sha256()
                    mode = 'wb'

                with open(partial_path, mode) as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)            # ファイルに書き込み
                            sha256_hash.update(chunk) # 同時にハッシュを更新

                orig_hash = model_inf["SHA256"].lower()
                this_hash = sha256_hash.hexdigest().lower()
                if orig_hash == this_hash:
                    shutil.move(partial_path, model_inf["saved_as"])
                    model_inf["result"] = "success"
                    return model_inf

                model_inf["result"] = f"error: hash mismatch {orig_hash} != {this_hash}"
                return model_inf
                
            except requests.HTTPError as e:
                stc = e.response.status_code
                if stc not in RETRY_STATUS_CODES:
                    model_inf["result"] = "error: " + traceback.format_exc()
                    return model_inf

                if retry_count == (MAX_RETRY-1):
                    model_inf["result"] = "error: retry count reached limit: "  + traceback.format_exc()
                    return model_inf

                logger.warning("retryable error: will be resumed after 20 sec.\n\n %s: %s", stc, e)
                time.sleep(20)
                continue

            except Exception as e:
                model_inf["result"] = "error: " + traceback.format_exc()
                return model_inf

    model_inf["result"] = "error: unexpected error"
    return model_inf



running_dl = []

def check_done(do_not_dl:bool):
    finished = []
    for f in running_dl:
        if not f.done():
            continue
        finished.append(f)
        try:
            ret = f.result()
            if ret["result"] == "success":
                if not do_not_dl:
                    logger.info("### Finished: %s => %s", ret["file"], ret["saved_as"])
            elif ret["result"].startswith("info:"):
                logger.info("### %s: %s", ret["file"], ret["result"])
            else:
                logger.warning("%s: %s", ret["file"], ret["result"])
        except Exception as e:
            traceback.print_exc(5)
            logger.error("%s: %s", e.__class__.__name__, e)

    for f in finished:
        running_dl.remove(f)

def download_files(dl_list:list, do_not_dl:bool):
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        for entry in dl_list:
            if entry.get("skip", False):
                continue

            check_done(do_not_dl)
            while len(running_dl) >= args.max_workers:
                time.sleep(5)
                check_done(do_not_dl)

            match entry["type"]:
                case "hf":
                    future = executor.submit(download_from_hf, entry, do_not_dl)
                case "civitai":
                    future = executor.submit(download_from_civitai, entry, do_not_dl)
                case _:
                    logger.warning("unknown type: %s", entry["type"])
                    continue

            running_dl.append(future)

        while len(running_dl) > 0:
            time.sleep(3)
            check_done(do_not_dl)

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


args = None

def main():
    global args, pbar
    parse = argparse.ArgumentParser("model downloader")
    parse.add_argument("--enable", type=lambda x: x.split(","))
    parse.add_argument("--disable", type=lambda x: x.split(","))
    parse.add_argument("--list", action="store_true")
    parse.add_argument("--dry-run", action="store_true")
    parse.add_argument("--max-workers", default=2, type=int)
    parse.add_argument("--verbose", action="store_true")

    args = parse.parse_args()

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

        if disabled:
            logger.debug("mark files of disabled group as 'to be removed': %s", k)
            for f in files:
                f["to_be_removed"] = True

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
