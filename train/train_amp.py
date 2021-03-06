
import sys
sys.path.insert(0, '.')
import os
import os.path as osp
import random
import time
import argparse
import numpy as np
from tabulate import tabulate
import logging

import torch
import torch.nn as nn
import torch.distributed as dist
from torch.utils.data import DataLoader
import torch.cuda.amp as amp

import cv2
import numpy as np

from networks import model_factory
from configs import cfg_factory
from dataload.rexroth_cv2 import get_data_loader
from evaluate.evaluate import eval_model
from ohem_ce_loss import OhemCELoss
from lr_scheduler import WarmupPolyLrScheduler
from utils.meters import TimeMeter, AvgMeter
from utils.logger import setup_logger, print_log_msg

import gc

torch.manual_seed(123)
torch.cuda.manual_seed(123)
np.random.seed(123)
random.seed(123)
torch.backends.cudnn.deterministic = True

def parse_args():
    parse = argparse.ArgumentParser()
    parse.add_argument('--local_rank', dest='local_rank', type=int, default=-1, )
    parse.add_argument('--port', dest='port', type=int, default=44554, )
    parse.add_argument('--model', dest='model', type=str, default='bisenetv1', )
    parse.add_argument('--fintune-from',dest='finetune_from', type=str, default=None, )
    return parse.parse_args()

args = parse_args()
cfg = cfg_factory[args.model]

def set_model():
    net = model_factory[cfg.model_type](cfg.categories)
    if not args.finetune_from is None:
        net.load_state_dict(torch.load(args.finetune_from, map_location='cpu'))
    if cfg.use_sync_bn: net = nn.SyncBatchNorm.convert_sync_batchnorm(net)
    net.cuda()
    net.train()
    criteria_pre = OhemCELoss(0.7)
    # criteria_pre = nn.BCEWithLogitsLoss() # loss change
    criteria_aux = [OhemCELoss(0.7) for _ in range(cfg.num_aux_heads)]
    # criteria_aux = [nn.BCEWithLogitsLoss() for _ in range(cfg.num_aux_heads)] # loss change
    return net, criteria_pre, criteria_aux

def set_optimizer(model):
    if hasattr(model, 'get_params'):
        wd_params, nowd_params, lr_mul_wd_params, lr_mul_nowd_params = model.get_params()
        params_list = [
            {'params': wd_params, },
            {'params': nowd_params, 'weight_decay': 0},
            {'params': lr_mul_wd_params, 'lr': cfg.lr_start * 10},
            {'params': lr_mul_nowd_params, 'weight_decay': 0, 'lr': cfg.lr_start * 10}
        ]
    else:
        wd_params, non_wd_params = [],[]
        for name, param in model.named_parameters():
            if param.dim() == 1:
                non_wd_params.append(param)
            elif param.dim() == 2 or param.dim() == 4:
                wd_params.append(param)
        params_list = [
            {'params': wd_params, },
            {'params': non_wd_params, 'weight_decay':0},
        ]
    optim = torch.optim.SGD(
        params_list, lr=cfg.lr_start, momentum=0.9, weight_decay=cfg.weight_decay,
    )
    return optim

def set_model_dist(net):
    local_rank = dist.get_rank()
    net = nn.parallel.DistributedDataParallel(
        net, device_ids=[local_rank, ], output_device=local_rank
    )
    return net

def set_meters():
    time_meter = TimeMeter(cfg.max_iter)
    loss_meter = AvgMeter('loss')
    loss_pre_meter = AvgMeter('loss_prem')
    loss_aux_meters = [AvgMeter('loss_aux{}'.format(i))
                       for i in range(cfg.num_aux_heads)]
    return time_meter, loss_meter, loss_pre_meter, loss_aux_meters

def train():
    logger = logging.getLogger()
    is_dist = dist.is_initialized()

    dl = get_data_loader(
        cfg.im_root, cfg.train_im_anns,
        cfg.ims_per_gpu, cfg.scales, cfg.cropsize,
        cfg.max_iter, mode='train', distributed=is_dist
    )

    net, criteria_pre, criteria_aux = set_model()

    optim = set_optimizer(net)

    scaler = amp.GradScaler()

    net = set_model_dist(net)

    time_meter, loss_meter, loss_pre_meter, loss_aux_meters = set_meters()

    lr_schdr = WarmupPolyLrScheduler(optim, power=0.9, max_iter=cfg.max_iter, warmup_iter=cfg.warmup_iters, warmup_ratio=0.1, warmup='exp', last_epoch=-1)

    i = 0

    for it, (im, lb) in enumerate(dl):
        im = im.cuda()
        lb= lb.cuda()

        lb = torch.squeeze(lb, 1)


        optim.zero_grad()
        with amp.autocast(enabled=cfg.use_fp16):
            logits, *logits_aux = net(im)

            # if i==0:
            #     print("pred tensorshape ", logits.shape)
            #     print("label tensor shape", lb.shape)
            #
            #     print("before argmax", logits[0].shape)
            #     guess_np = logits[0].argmax(dim=0)
            #     print("before squeeze", guess_np.shape)
            #     guess_np = guess_np.squeeze().detach().cpu().numpy()
            #     print("after squeeze", guess_np.shape)
            #     lable = lb[0].detach().cpu().numpy()
            #     print(guess_np.dtype)
            #     print(lable.dtype)
            #
            #     palette = np.random.randint(0, 256, (256, 3), dtype=np.uint8)
            #     guess_np = palette[guess_np]
            #     lable = palette[lable]
            #     sample_image = im[0].detach().cpu().numpy()
            #     print("train image dim:", sample_image.shape)
            #     # cv2.imwrite('image.png', sample_image)
            #     cv2.imwrite('pred.jpg', guess_np)
            #     cv2.imwrite("label.jpg", lable)
            #
            #     ## write single prediction and label to 2 seperate files
            #     pred = logits[0].detach().cpu().numpy()
            #     img = im[0].detach().cpu().numpy()
            #     print(img.shape)
            #     # print(pred)
            #     labeleee = lb[0].detach().cpu().numpy()
            #     with open('pred.txt', 'w') as outfile:
            #         for slice_2d in pred:
            #             np.savetxt(outfile, slice_2d)
            #     np.savetxt('label.txt', labeleee)
            #     with open('img.txt', 'w') as infile:
            #         for slice_2d in img:
            #             np.savetxt(infile, slice_2d)


            loss_pre = criteria_pre(logits, lb)
            loss_aux = [crit(lgt, lb) for crit, lgt in zip(criteria_aux, logits_aux)]
            loss = loss_pre + sum(loss_aux)


        scaler.scale(loss).backward()
        scaler.step(optim)
        scaler.update()
        print ("step 1")
        torch.cuda.synchronize()

        time_meter.update()
        loss_meter.update(loss.item())
        loss_pre_meter.update(loss_pre.item())
        _ = [mter.update(lss.item()) for mter, lss in zip(loss_aux_meters, loss_aux)]

        if (it + 1) % 100 == 0:
            lr = lr_schdr.get_lr()
            lr = sum(lr) / len(lr)
            print_log_msg(
                it, cfg.max_iter, lr, time_meter, loss_meter, loss_pre_meter, loss_aux_meters)
        lr_schdr.step()
        print("step 2")
        i = i + 1


    save_pth = osp.join(cfg.respth, 'model_final.pth')
    logger.info('\nsave models to {}'.format(save_pth))
    state = net.module.state_dict()
    if dist.get_rank() == 0: torch.save(state, save_pth)

    logger.info('\nevaluating the final model')
    torch.cuda.empty_cache()
    heads, mious = eval_model(net, 2, cfg.im_root, cfg.val_im_anns)
    logger.info(tabulate([mious, ], headers=heads, tablefmt='orgtbl'))

    return

def main():
    torch.cuda.set_device(args.local_rank)
    dist.init_process_group(
        backend='nccl',
        init_method='tcp://127.0.0.1:{}'.format(args.port),
        world_size=torch.cuda.device_count(),
        rank = args.local_rank
    )
    if not osp.exists(cfg.respth): os.makedirs(cfg.respth)
    setup_logger('{}-train'.format(cfg.model_type), cfg.respth)
    train()

if __name__ == "__main__":
    main()





