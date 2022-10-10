"""
utils function for ymir and yolov5
"""
import os.path as osp
import shutil
from typing import Any, List

import numpy as np
import torch
import yaml
from easydict import EasyDict as edict
from nptyping import NDArray, Shape, UInt8
from ymir_exc import env
from ymir_exc import monitor
from ymir_exc import result_writer as rw
from ymir_exc.util import YmirStage, get_bool, get_weight_files, get_ymir_process

from models.common import DetectMultiBackend
from utils.augmentations import letterbox
from utils.general import check_img_size, non_max_suppression, scale_boxes
from utils.torch_utils import select_device

BBOX = NDArray[Shape['*,4'], Any]
CV_IMAGE = NDArray[Shape['*,*,3'], UInt8]


def get_weight_file(cfg: edict) -> str:
    """
    return the weight file path by priority
    find weight file in cfg.param.model_params_path or cfg.param.model_params_path
    """
    weight_files = get_weight_files(cfg, suffix=('.pt'))
    # choose weight file by priority, best.pt > xxx.pt
    for p in weight_files:
        if p.endswith('best.pt'):
            return p

    if len(weight_files) > 0:
        return max(weight_files, key=osp.getctime)

    return ""


class YmirYolov5(torch.nn.Module):
    """
    used for mining and inference to init detector and predict.
    """
    def __init__(self, cfg: edict, task='infer'):
        super().__init__()
        self.cfg = cfg
        if cfg.ymir.run_mining and cfg.ymir.run_infer:
            # multiple task, run mining first, infer later
            if task == 'infer':
                self.task_idx = 1
            elif task == 'mining':
                self.task_idx = 0
            else:
                raise Exception(f'unknown task {task}')

            self.task_num = 2
        else:
            self.task_idx = 0
            self.task_num = 1

        self.gpu_id: str = str(cfg.param.get('gpu_id', '0'))
        device = select_device(self.gpu_id)  # will set CUDA_VISIBLE_DEVICES=self.gpu_id
        self.gpu_count: int = len(self.gpu_id.split(',')) if self.gpu_id else 0
        self.batch_size_per_gpu: int = int(cfg.param.get('batch_size_per_gpu', 4))
        self.num_workers_per_gpu: int = int(cfg.param.get('num_workers_per_gpu', 4))
        self.pin_memory: bool = get_bool(cfg, 'pin_memory', False)
        self.batch_size: int = self.batch_size_per_gpu * self.gpu_count
        self.model = self.init_detector(device)
        self.model.eval()
        self.device = device
        self.class_names: List[str] = cfg.param.class_names
        self.stride = self.model.stride
        self.conf_thres: float = float(cfg.param.conf_thres)
        self.iou_thres: float = float(cfg.param.iou_thres)

        # view convert_ymir_to_yolov5() for detail
        cache_dir = cfg.param.get('cache_dir', '') or cfg.ymir.output.root_dir
        self.data_yaml = osp.join(cache_dir, 'data.yaml')

        img_size = int(cfg.param.img_size)
        imgsz = [img_size, img_size]
        imgsz = check_img_size(imgsz, s=self.stride)

        self.model.warmup(imgsz=(1, 3, *imgsz))  # warmup
        self.img_size: List[int] = imgsz

    def extract_feats(self, x):
        """
        return the feature maps before sigmoid for mining
        """
        return self.model.model(x)[1]

    def forward(self, x, nms=False):
        pred = self.model(x)
        if not nms:
            return pred

        pred = non_max_suppression(
            pred,
            conf_thres=self.conf_thres,
            iou_thres=self.iou_thres,
            classes=None,  # not filter class_idx
            agnostic=False,
            max_det=100)
        return pred

    def init_detector(self, device: torch.device) -> DetectMultiBackend:
        weights = get_weight_file(self.cfg)

        if not weights:
            raise Exception("no weights file specified!")

        model = DetectMultiBackend(
            weights=weights,
            device=device,
            dnn=False,  # not use opencv dnn for onnx inference
            data=self.data_yaml)  # dataset.yaml path

        return model

    def predict(self, img: CV_IMAGE) -> NDArray:
        """
        predict single image and return bbox information
        img: opencv BGR, uint8 format
        """
        # preprocess: padded resize
        img1 = letterbox(img, self.img_size, stride=self.stride, auto=True)[0]

        # preprocess: convert data format
        img1 = img1.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
        img1 = np.ascontiguousarray(img1)
        img1 = torch.from_numpy(img1).to(self.device)

        img1 = img1 / 255  # 0 - 255 to 0.0 - 1.0
        img1.unsqueeze_(dim=0)  # expand for batch dim
        pred = self.forward(img1, nms=True)

        result = []
        for det in pred:
            if len(det):
                # Rescale boxes from img_size to img size
                det[:, :4] = scale_boxes(img1.shape[2:], det[:, :4], img.shape).round()
                result.append(det)

        # xyxy, conf, cls
        if len(result) > 0:
            tensor_result = torch.cat(result, dim=0)
            numpy_result = tensor_result.data.cpu().numpy()
        else:
            numpy_result = np.zeros(shape=(0, 6), dtype=np.float32)

        return numpy_result

    def infer(self, img: CV_IMAGE) -> List[rw.Annotation]:
        anns = []
        result = self.predict(img)

        for i in range(result.shape[0]):
            xmin, ymin, xmax, ymax, conf, cls = result[i, :6].tolist()
            ann = rw.Annotation(class_name=self.class_names[int(cls)],
                                score=conf,
                                box=rw.Box(x=int(xmin), y=int(ymin), w=int(xmax - xmin), h=int(ymax - ymin)))

            anns.append(ann)

        return anns

    def write_monitor_logger(self, stage: YmirStage, p: float):
        monitor.write_monitor_logger(
            percent=get_ymir_process(stage=stage, p=p, task_idx=self.task_idx, task_num=self.task_num))


def convert_ymir_to_yolov5(cfg: edict) -> str:
    """
    convert ymir format dataset to yolov5 format
    generate data.yaml for training/mining/infer
    cache to other docker images for better speed when use nfs-like file-system
    """

    cache_dir = cfg.param.get('cache_dir', '') or cfg.ymir.output.root_dir
    data = dict(path=cache_dir,
                nc=len(cfg.param.class_names),
                names={idx: name
                       for idx, name in enumerate(cfg.param.class_names)})
    for split, prefix in zip(['train', 'val', 'test'], ['training', 'val', 'candidate']):
        src_file = getattr(cfg.ymir.input, f'{prefix}_index_file')
        if osp.exists(src_file):
            shutil.copy(src_file, f'{cache_dir}/{split}.tsv')

        data[split] = f'{split}.tsv'

    data_yaml = osp.join(cache_dir, 'data.yaml')
    with open(data_yaml, 'w') as fw:
        fw.write(yaml.safe_dump(data))
    return data_yaml
