# bisenet v1
cfg = dict(
    model_type='bisenetv1',
    num_aux_heads=2,
    aux_output=True,
    lr_start=1e-2,
    lr_multiplier=10,
    weight_decay=5e-4,
    warmup_iters=200,
    max_iter=6400,
    im_root='./datasets/Rexroth',
    train_im_anns='./datasets/Rexroth/train.txt',
    val_im_anns='./datasets/Rexroth/eval.txt',
    scales=[0.5, 2.0],
    cropsize=[480, 640],
    ims_per_gpu=16,
    use_fp16=True,
    use_sync_bn=False,
    respth='./res',
    categories=3,
    save_name='bisenet_v1.pth',
    )