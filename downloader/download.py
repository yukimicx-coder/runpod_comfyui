import os
import sys
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import hashlib
import time
from tqdm.auto import tqdm
import shutil

os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
from huggingface_hub import hf_hub_download, HfApi

import logging
import traceback

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
lh = logging.StreamHandler()
logger.addHandler(lh)

CIVITAI_KEY=os.environ.get("CIVITAI_KEY", "")
CIVITAI_API_ROOT=os.environ.get("CIVITAI_API_ROOT", "https://civitai.com/api/v1")

model_list_file = os.environ.get("MODEL_LIST_FILE", "/model_list.yaml")
models_root = os.environ.get("MODELS_ROOT", "/workspace/models")

if not os.path.isfile(model_list_file):
    logger.debug(f"file not found: {model_list_file}")
    sys.exit(1)

yml = None
with open(model_list_file, "r") as mlist:
    yml = yaml.safe_load(mlist)

if not isinstance(yml, list):
    logger.debug(f"invalid file format: {model_list_file}")
    sys.exit(2)

def compare_hashes(local_path, hash_hex:str):
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b''):
            h.update(chunk)
    this_hash = h.hexdigest().lower()
    orig_hash = hash_hex.lower()
    #logger.debug(f"orig={orig_hash}, this={this_hash}")
    return this_hash == orig_hash

def hf_get_file_hash(kwargs):
    hf_api = HfApi()
    file_path = kwargs["filename"]
    if "subfolder" in kwargs:
        file_path = os.path.join(kwargs["subfolder"], file_path)
    info = hf_api.get_paths_info(repo_id=kwargs["repo_id"], paths=[file_path])[0]
    if info.lfs:
        return info.lfs["sha256"].lower()
    return None

def download_from_hf(model_inf:dict):
    if "/" in model_inf['file']:
        sub_dir = os.path.dirname(model_inf["file"])
        model_inf["file"] = os.path.basename(model_inf["file"])
        if model_inf.get("subdir", sub_dir) != sub_dir:
            logger.warning("conflict 'subdir' and dirname of 'file': %s != %s", model_inf["subdir"], sub_dir)
        model_inf["subdir"] = sub_dir

    local_dir = os.path.join(models_root, model_inf["ldir"])
    os.makedirs(local_dir, exist_ok=True)

    kwargs = {
        "repo_id": model_inf["repo"],
        "filename": model_inf['file'],
        "local_dir": local_dir
    }

    if model_inf.get("subdir"):
        kwargs["subfolder"] = model_inf["subdir"]

    save_path = os.path.join(local_dir, model_inf.get("lfile", model_inf["file"]))

    if os.path.isfile(save_path):
        if not model_inf.get("overwrite", False):
            if compare_hashes(save_path, hf_get_file_hash(kwargs)):
                model_inf["result"] = "同一ファイルスキップ"
                return model_inf

    logger.info("hf start downloading: %s", model_inf['file'])
    try:

        # 個別ファイルをダウンロード
        dl_path = hf_hub_download(**kwargs)

        if save_path != dl_path:
            shutil.move(dl_path, save_path)

        model_inf["result"] = "success"
        model_inf["saved_as"] = save_path
        return model_inf

    except Exception as e:
        traceback.print_exc(5)
        model_inf["result"] = f"エラー: {e.__class__.__name__}: {e}"
        return model_inf

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRY=3
CHUNK_SIZE=8192
def download_from_civitai(model_inf:dict):
    url_path = f"/model-versions/mini/{model_inf['version-id']}"
    headers = {"Authorization": f"Bearer {CIVITAI_KEY}"}

    logger.debug("civitai: start get info: %s", url_path)

    try:
        response = requests.get(CIVITAI_API_ROOT + url_path, headers=headers)
        response.raise_for_status()
    except Exception as e:
        traceback.print_exc(5)
        model_inf["result"] = f"情報取得エラー: {url_path}: {e.__class__.__name__}: {e}"
        return model_inf

    versions_data = response.json()
    # 最初に見つかったファイルのダウンロードリンクを取得
    # 量子化の違うファイル取得にはminiじゃないフル情報が必要になる
    file_url = versions_data["downloadUrls"][0]
    model_inf["file"] = versions_data['fileName']

    local_dir = os.path.join(models_root, model_inf["ldir"])
    os.makedirs(local_dir, exist_ok=True)

    save_path = os.path.join(local_dir, model_inf.get("lfile", model_inf["file"]))

    if os.path.isfile(save_path):
        if not model_inf.get("overwrite", False):
            if compare_hashes(save_path, versions_data["hashes"]["SHA256"]):
                model_inf["result"] = "同一ファイルスキップ"
                return model_inf


    partial_path = save_path + ".partial"

    sha256_hash = hashlib.sha256()
    if os.path.isfile(partial_path):
        # 中断ファイルがあればハッシュに含める
        with open(partial_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                sha256_hash.update(chunk)

    session = requests.Session()

    logger.debug("civitai: start downloading: %s", file_url)
    for _ in range(MAX_RETRY):
        part_len = 0
        if os.path.isfile(partial_path):
            part_len = os.path.getsize(partial_path)
            headers["Range"] = f'bytes={part_len}-'
            logger.debug("civitai: request resuming: %s", file_url)

        # ファイル本体のダウンロード
        with session.get(file_url, headers=headers, stream=True, timeout=30) as response:
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

                orig_hash = versions_data["hashes"]["SHA256"].lower()
                this_hash = sha256_hash.hexdigest().lower()
                if orig_hash == this_hash:
                    shutil.move(partial_path, save_path)
                    model_inf["result"] = "success"
                    model_inf["saved_as"] = save_path
                    return model_inf

                model_inf["result"] = f"ハッシュ値不一致エラー {orig_hash} != {this_hash}: {file_url}"
                return model_inf
                
            except requests.HTTPError as e:
                stc = e.response.status_code
                if stc not in RETRY_STATUS_CODES:
                    logger.error("続行不能なエラー: %s", stc)
                    raise
                logger.warning("エラー発生(20秒後に再開): %s: %s", stc, e)
                time.sleep(20)
                continue

    model_inf["result"] = f"エラー: 試行回数がMAX_RETRYに達した: {file_url}"
    return model_inf

def check_done():
    global running_dl

    finished = []
    for f in running_dl:
        if not f.done():
            continue
        finished.append(f)
        try:
            ret = f.result()
            if ret["result"] == "success":
                logger.info("完了: %s => %s", ret["file"], ret["saved_as"])
            else:
                logger.warning("%s: %s" % (ret["file"], ret["result"]))
        except Exception as e:
            traceback.print_exc(5)
            logger.error("%s: %s", e.__class__.__name__, e)

    for f in finished:
        running_dl.remove(f)


MAX_WORKERS = 4
running_dl = []
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    for model in yml:
        if model.get("skip", False):
            continue

        check_done()
        while len(running_dl) >= MAX_WORKERS:
            time.sleep(5)
            check_done()

        if model["type"] == "hf":
            future = executor.submit(download_from_hf, model)
        elif model["type"] == "civitai":
            future = executor.submit(download_from_civitai, model)
        else:
            logger.warning("unexpected type: %s", model["type"])
            continue

        running_dl.append(future)

    while len(running_dl) > 0:
        time.sleep(5)
        check_done()
