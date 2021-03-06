import sys
sys.path.insert(0, '.')
import argparse
import torch
import torch.nn as nn
from PIL import Image
import numpy as np
import cv2

import dataload.transform_cv2 as T
from networks import model_factory
from configs import cfg_factory

torch.set_grad_enabled(False)
np.random.seed(123)

parse = argparse.ArgumentParser()
parse.add_argument('--model', dest='model', type=str, default='fanet18_v4_se2',)
parse.add_argument('--weight-path', dest='weight_path', type=str, default='./res/fanet18_v4_se2/fanet18_v4_se2.pth12000',)
parse.add_argument('--img-path', dest='img_path', type=str, default='./2630.png',)
args = parse.parse_args()
cfg = cfg_factory[args.model]

palette = np.random.randint(0,256, (256, 3), dtype=np.uint8)

net = model_factory[cfg.model_type](cfg.categories, aux_output=False, export=False)
net.load_state_dict(torch.load(args.weight_path, map_location='cpu'), strict=False)
net.eval()
net.cuda()

to_tensor = T.ToTensor(
    mean=(0.3257, 0.3690, 0.3223),
    std=(0.2112, 0.2148, 0.2115),
)
im = cv2.imread(args.img_path)[:, :, ::-1]
im = to_tensor(dict(im=im, lb=None))['im'].unsqueeze(0).cuda()

out = net(im)
print(out.shape)
out = out.argmax(dim=1).squeeze().detach().cpu().numpy()
pred = palette[out]
cv2.imwrite('./res/fanet18_v4_se2/fanet18_v4_se2_test.png', pred)
