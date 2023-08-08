import argparse
import csv
import glob
import os

from PIL import Image
import cv2
from tqdm import tqdm
import numpy as np
from tensorflow.keras.models import load_model
from huggingface_hub import hf_hub_download
import torch
from pathlib import Path

import sd_scripts.library.train_util as train_util

# from wd14 tagger
IMAGE_SIZE = 448

# wd-v1-4-swinv2-tagger-v2 / wd-v1-4-vit-tagger / wd-v1-4-vit-tagger-v2/ wd-v1-4-convnext-tagger / wd-v1-4-convnext-tagger-v2
DEFAULT_WD14_TAGGER_REPO = "SmilingWolf/wd-v1-4-convnext-tagger-v2"
FILES = ["keras_metadata.pb", "saved_model.pb", "selected_tags.csv"]
SUB_DIR = "variables"
SUB_DIR_FILES = ["variables.data-00000-of-00001", "variables.index"]
CSV_FILE = FILES[-1]


def preprocess_image(image):
    image = np.array(image)
    image = image[:, :, ::-1]  # RGB->BGR

    # pad to square
    size = max(image.shape[0:2])
    pad_x = size - image.shape[1]
    pad_y = size - image.shape[0]
    pad_l = pad_x // 2
    pad_t = pad_y // 2
    image = np.pad(image, ((pad_t, pad_y - pad_t), (pad_l, pad_x - pad_l), (0, 0)), mode="constant",
                   constant_values=255)

    interp = cv2.INTER_AREA if size > IMAGE_SIZE else cv2.INTER_LANCZOS4
    image = cv2.resize(image, (IMAGE_SIZE, IMAGE_SIZE), interpolation=interp)

    image = image.astype(np.float32)
    return image


class ImageLoadingPrepDataset(torch.utils.data.Dataset):
    def __init__(self, image_paths):
        self.images = image_paths

    def __len__(self):
        return len(self.images)

    def __getitem__(self, idx):
        img_path = str(self.images[idx])

        try:
            image = Image.open(img_path).convert("RGB")
            image = preprocess_image(image)
            tensor = torch.tensor(image)
        except Exception as e:
            print(f"Could not load image path / 画像を読み込めません: {img_path}, error: {e}")
            return None

        return (tensor, img_path)


def collate_fn_remove_corrupted(batch):
    """Collate function that allows to remove corrupted examples in the
    dataloader. It expects that the dataloader returns 'None' when that occurs.
    The 'None's in the batch are removed.
    """
    # Filter out all the Nones (corrupted examples)
    batch = list(filter(lambda x: x is not None, batch))
    return batch


# class TaggerModel:
#     def __init__(self) -> None:
#         self.loaded = False
#         self.model = None
#     def get_model(self,path):
#         if self.model is None:
#             # from onnxruntime import InferenceSession

#             # https://onnxruntime.ai/docs/execution-providers/
#             # https://github.com/toriato/stable-diffusion-webui-wd14-tagger/commit/e4ec460122cf674bbf984df30cdb10b4370c1224#r92654958
#             # providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
#             # providers.pop(0)

#             # self.model = InferenceSession(str(path), providers=providers)
#             self.model = load_model(path)
#         return self.model
#     def del_model(self):
#         del self.model
#         self.model=None


def main(train_data_dir="", # 训练数据路径
    repo_id=DEFAULT_WD14_TAGGER_REPO, # wd模型在huggingface上面的repo id
    model_dir="", # wd模型的地址
    force_download=False, # 没有wd的模型时是否强制下载
    batch_size=8, # 一次处理多少张图片
    max_data_loader_n_workers=1, # 使用多少个worker来同时读取数据
    caption_extension=".txt", # 存tag的文件类型
    general_threshold=0.35, # 为一般类别添加标签的置信阈值
    character_threshold=0.35, # 为字符类别添加标签的置信阈值
    recursive=True, # 搜索子文件夹中的图像
    remove_underscore=True, # 将输出标记的下划线替换为空格
    undesired_tags="", # 不想要（想要去除）的Tag，以英文逗号隔开
    frequency_tags=False, # 显示tag的频率
    addtional_tags="",):
    # hf_hub_downloadをそのまま使うとsymlink関係で問題があるらしいので、キャッシュディレクトリとforce_filenameを指定してなんとかする
    # depreacatedの警告が出るけどなくなったらその時
    # https://github.com/toriato/stable-diffusion-webui-wd14-tagger/issues/22

    # tagger_model = TaggerModel()
    if not os.path.exists(model_dir) or force_download:
        print(f"downloading wd14 tagger model from hf_hub. id: {repo_id}")
        for file in FILES:
            hf_hub_download(repo_id, file, cache_dir=model_dir, force_download=True, force_filename=file)
        for file in SUB_DIR_FILES:
            hf_hub_download(
                repo_id,
                file,
                subfolder=SUB_DIR,
                cache_dir=os.path.join(model_dir, SUB_DIR),
                force_download=True,
                force_filename=file,
            )
    else:
        print("using existing wd14 tagger model")

    # 画像を読み込む
    model = load_model(model_dir)
    # model = tagger_model.get_model(args.model_dir)

    # label_names = pd.read_csv("2022_0000_0899_6549/selected_tags.csv")
    # 依存ライブラリを増やしたくないので自力で読むよ

    with open(os.path.join(model_dir, CSV_FILE), "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        l = [row for row in reader]
        header = l[0]  # tag_id,name,category,count
        rows = l[1:]
    assert header[0] == "tag_id" and header[1] == "name" and header[2] == "category", f"unexpected csv format: {header}"

    general_tags = [row[1] for row in rows[1:] if row[2] == "0"]
    character_tags = [row[1] for row in rows[1:] if row[2] == "4"]

    # 画像を読み込む

    train_data_dir_path = Path(train_data_dir)
    image_paths = train_util.glob_images_pathlib(train_data_dir_path, recursive)
    print(f"found {len(image_paths)} images.")

    tag_freq = {}

    undesired_tags = set(undesired_tags.split(","))
    general_tags = addtional_tags.split(",") + general_tags
    def run_batch(path_imgs):
        imgs = np.array([im for _, im in path_imgs])

        probs = model(imgs, training=False)
        probs = probs.numpy()

        for (image_path, _), prob in zip(path_imgs, probs):
            # 最初の4つはratingなので無視する
            # # First 4 labels are actually ratings: pick one with argmax
            # ratings_names = label_names[:4]
            # rating_index = ratings_names["probs"].argmax()
            # found_rating = ratings_names[rating_index: rating_index + 1][["name", "probs"]]

            # それ以降はタグなのでconfidenceがthresholdより高いものを追加する
            # Everything else is tags: pick any where prediction confidence > threshold
            combined_tags = []
            general_tag_text = ""
            character_tag_text = ""
            for i, p in enumerate(prob[4:]):
                if i < len(general_tags) and p >= general_threshold:
                    tag_name = general_tags[i]
                    if remove_underscore and len(tag_name) > 3:  # ignore emoji tags like >_< and ^_^
                        tag_name = tag_name.replace("_", " ")

                    if tag_name not in undesired_tags:
                        tag_freq[tag_name] = tag_freq.get(tag_name, 0) + 1
                        general_tag_text += ", " + tag_name
                        combined_tags.append(tag_name)
                elif i >= len(general_tags) and p >= character_threshold:
                    tag_name = character_tags[i - len(general_tags)]
                    if remove_underscore and len(tag_name) > 3:
                        tag_name = tag_name.replace("_", " ")

                    if tag_name not in undesired_tags:
                        tag_freq[tag_name] = tag_freq.get(tag_name, 0) + 1
                        character_tag_text += ", " + tag_name
                        combined_tags.append(tag_name)

            # 先頭のカンマを取る
            if len(general_tag_text) > 0:
                general_tag_text = general_tag_text[2:]
            if len(character_tag_text) > 0:
                character_tag_text = character_tag_text[2:]

            tag_text = ", ".join(combined_tags)
            # tag_text = args.addtional_tags +","+ tag_text

            with open(os.path.splitext(image_path)[0] + caption_extension, "wt", encoding="utf-8") as f:
                f.write(tag_text + "\n")
                # if debug:
                #     print(f"\n{image_path}:\n  Character tags: {character_tag_text}\n  General tags: {general_tag_text}")

    # 読み込みの高速化のためにDataLoaderを使うオプション
    if max_data_loader_n_workers is not None:
        dataset = ImageLoadingPrepDataset(image_paths)
        data = torch.utils.data.DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=max_data_loader_n_workers,
            collate_fn=collate_fn_remove_corrupted,
            drop_last=False,
        )
    else:
        data = [[(None, ip)] for ip in image_paths]

    b_imgs = []
    for data_entry in tqdm(data, smoothing=0.0):
        for data in data_entry:
            if data is None:
                continue

            image, image_path = data
            if image is not None:
                image = image.detach().numpy()
            else:
                try:
                    image = Image.open(image_path)
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    image = preprocess_image(image)
                except Exception as e:
                    print(f"Could not load image path / 画像を読み込めません: {image_path}, error: {e}")
                    continue
            b_imgs.append((image_path, image))

            if len(b_imgs) >= batch_size:
                b_imgs = [(str(image_path), image) for image_path, image in b_imgs]  # Convert image_path to string
                run_batch(b_imgs)
                b_imgs.clear()

    if len(b_imgs) > 0:
        b_imgs = [(str(image_path), image) for image_path, image in b_imgs]  # Convert image_path to string
        run_batch(b_imgs)

    if frequency_tags:
        sorted_tags = sorted(tag_freq.items(), key=lambda x: x[1], reverse=True)
        print("\nTag frequencies:")
        for tag, freq in sorted_tags:
            print(f"{tag}: {freq}")
    del model
    del general_tags
    del character_tags
    # tagger_model.del_model()
    print("done!")


# def setup_parser() -> argparse.ArgumentParser:
#     parser = argparse.ArgumentParser()
#     parser.add_argument("train_data_dir", type=str, default=None,help="directory for train images / 学習画像データのディレクトリ")
#     parser.add_argument(
#         "--repo_id",
#         type=str,
#         default=DEFAULT_WD14_TAGGER_REPO,
#         help="repo id for wd14 tagger on Hugging Face / Hugging Faceのwd14 taggerのリポジトリID",
#     )
#     parser.add_argument(
#         "--model_dir",
#         type=str,
#         default="wd14_tagger_model",
#         help="directory to store wd14 tagger model / wd14 taggerのモデルを格納するディレクトリ",
#     )
#     parser.add_argument(
#         "--force_download", action="store_true", help="force downloading wd14 tagger models / wd14 taggerのモデルを再ダウンロードします"
#     )
#     parser.add_argument("--batch_size", type=int, default=1, help="batch size in inference / 推論時のバッチサイズ")
#     parser.add_argument(
#         "--max_data_loader_n_workers",
#         type=int,
#         default=None,
#         help="enable image reading by DataLoader with this number of workers (faster) / DataLoaderによる画像読み込みを有効にしてこのワーカー数を適用する（読み込みを高速化）",
#     )
#     parser.add_argument(
#         "--caption_extention",
#         type=str,
#         default=None,
#         help="extension of caption file (for backward compatibility) / 出力されるキャプションファイルの拡張子（スペルミスしていたのを残してあります）",
#     )
#     parser.add_argument("--caption_extension", type=str, default=".txt", help="extension of caption file / 出力されるキャプションファイルの拡張子")
#     parser.add_argument("--thresh", type=float, default=0.35, help="threshold of confidence to add a tag / タグを追加するか判定する閾値")
#     parser.add_argument(
#         "--general_threshold",
#         type=float,
#         default=None,
#         help="threshold of confidence to add a tag for general category, same as --thresh if omitted / generalカテゴリのタグを追加するための確信度の閾値、省略時は --thresh と同じ",
#     )
#     parser.add_argument(
#         "--character_threshold",
#         type=float,
#         default=None,
#         help="threshold of confidence to add a tag for character category, same as --thres if omitted / characterカテゴリのタグを追加するための確信度の閾値、省略時は --thresh と同じ",
#     )
#     parser.add_argument("--recursive", action="store_true", help="search for images in subfolders recursively / サブフォルダを再帰的に検索する")
#     parser.add_argument(
#         "--remove_underscore",
#         action="store_true",
#         help="replace underscores with spaces in the output tags / 出力されるタグのアンダースコアをスペースに置き換える",
#     )
#     parser.add_argument("--debug", action="store_true", help="debug mode")
#     parser.add_argument(
#         "--undesired_tags",
#         type=str,
#         default="",
#         help="comma-separated list of undesired tags to remove from the output / 出力から除外したいタグのカンマ区切りのリスト",
#     )
#     parser.add_argument("--frequency_tags", action="store_true", help="Show frequency of tags for images / 画像ごとのタグの出現頻度を表示する")

#     parser.add_argument("--addtional_tags", type=str,
#         default="",
#         help="需要添加的额外提示词")

#     return parser


# if __name__ == "__main__":
#     parser = setup_parser()

#     args = parser.parse_args()

#     # スペルミスしていたオプションを復元する
#     if args.caption_extention is not None:
#         args.caption_extension = args.caption_extention

#     if args.general_threshold is None:
#         args.general_threshold = args.thresh
#     if args.character_threshold is None:
#         args.character_threshold = args.thresh

#     main(args)


def tagger(
    train_data_dir="", # 训练数据路径
    repo_id=DEFAULT_WD14_TAGGER_REPO, # wd模型在huggingface上面的repo id
    model_dir="", # wd模型的地址
    force_download=False, # 没有wd的模型时是否强制下载
    batch_size=8, # 一次处理多少张图片
    max_data_loader_n_workers=1, # 使用多少个worker来同时读取数据
    caption_extension=".txt", # 存tag的文件类型
    general_threshold=0.35, # 为一般类别添加标签的置信阈值
    character_threshold=0.35, # 为字符类别添加标签的置信阈值
    recursive=True, # 搜索子文件夹中的图像
    remove_underscore=True, # 将输出标记的下划线替换为空格
    undesired_tags="", # 不想要（想要去除）的Tag，以英文逗号隔开
    frequency_tags=False, # 显示tag的频率
    addtional_tags="",
):

    # parser = setup_parser()
    # args = parser.parse_args(["--train_data_dir /data/qll/pics/"])
    # args.train_data_dir = train_data_dir
    # args.repo_id = repo_id
    # args.model_dir = model_dir
    # args.force_download = force_download
    # args.batch_size = batch_size
    # args.max_data_loader_n_workers = max_data_loader_n_workers
    # args.caption_extension = caption_extension
    # args.general_threshold = general_threshold
    # args.character_threshold = character_threshold
    # args.recursive = recursive
    # args.remove_underscore = remove_underscore
    # args.undesired_tags = undesired_tags
    # args.frequency_tags = frequency_tags
    # args.addtional_tags = addtional_tags
    main(train_data_dir = train_data_dir,
    repo_id = repo_id,
    model_dir = model_dir,
    force_download = force_download,
    batch_size = batch_size,
    max_data_loader_n_workers = max_data_loader_n_workers,
    caption_extension = caption_extension,
    general_threshold = general_threshold,
    character_threshold = character_threshold,
    recursive = recursive,
    remove_underscore = remove_underscore,
    undesired_tags = undesired_tags,
    frequency_tags = frequency_tags,
    addtional_tags = addtional_tags)



if __name__ == "__main__":
    tagger(
        train_data_dir="/data/qll/pics/nakedxiong_embedding",
        model_dir="/data/qll/tagger_models/model.onnx",
        force_download=False,
        batch_size=1,
        max_data_loader_n_workers=1,
        caption_extension=".txt",
        general_threshold=0.35,
        character_threshold=0.35,
        recursive=True,
        remove_underscore=True,
        undesired_tags="",
        frequency_tags=False,
        addtional_tags=""
    )
