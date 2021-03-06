import warnings

import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import ConvModule, xavier_init
from mmcv.runner import auto_fp16
from .attention_module.fusion_attention import FusionAttention
from .attention_module.context_fusion_block import ContextBlock
from .attention_module.context_weight_block import ContextWeightBlcok
from .attention_module.attention_augmented import AugmentedAttention
from .attention_module.self_attention import SelfAttention
from ..builder import NECKS
import matplotlib.pyplot as plt


@NECKS.register_module()
class Attention(nn.Module):
    r"""Feature Pyramid Network.

    This is an implementation of paper `Feature Pyramid Networks for Object
    Detection <https://arxiv.org/abs/1612.03144>`_.

    Args:
        in_channels (List[int]): Number of input channels per scale.
        out_channels (int): Number of output channels (used at each scale)
        num_outs (int): Number of output scales.
        start_level (int): Index of the start input backbone level used to
            build the feature pyramid. Default: 0.
        end_level (int): Index of the end input backbone level (exclusive) to
            build the feature pyramid. Default: -1, which means the last level.
        add_extra_convs (bool | str): If bool, it decides whether to add conv
            layers on top of the original feature maps. Default to False.
            If True, its actual mode is specified by `extra_convs_on_inputs`.
            If str, it specifies the source feature map of the extra convs.
            Only the following options are allowed

            - 'on_input': Last feat map of neck inputs (i.e. backbone feature).
            - 'on_lateral':  Last feature map after lateral convs.
            - 'on_output': The last output feature map after fpn convs.
        extra_convs_on_inputs (bool, deprecated): Whether to apply extra convs
            on the original feature from the backbone. If True,
            it is equivalent to `add_extra_convs='on_input'`. If False, it is
            equivalent to set `add_extra_convs='on_output'`. Default to True.
        relu_before_extra_convs (bool): Whether to apply relu before the extra
            conv. Default: False.
        no_norm_on_lateral (bool): Whether to apply norm on lateral.
            Default: False.
        conv_cfg (dict): Config dict for convolution layer. Default: None.
        norm_cfg (dict): Config dict for normalization layer. Default: None.
        act_cfg (str): Config dict for activation layer in ConvModule.
            Default: None.
        upsample_cfg (dict): Config dict for interpolate layer.
            Default: `dict(mode='nearest')`

    Example:
        >>> import torch
        >>> in_channels = [2, 3, 5, 7]
        >>> scales = [340, 170, 84, 43]
        >>> inputs = [torch.rand(1, c, s, s)
        ...           for c, s in zip(in_channels, scales)]
        >>> self = Attention(in_channels, 11, len(in_channels)).eval()
        >>> outputs = self.forward(inputs)
        >>> for i in range(len(outputs)):
        ...     print(f'outputs[{i}].shape = {outputs[i].shape}')
        outputs[0].shape = torch.Size([1, 11, 340, 340])
        outputs[1].shape = torch.Size([1, 11, 170, 170])
        outputs[2].shape = torch.Size([1, 11, 84, 84])
        outputs[3].shape = torch.Size([1, 11, 43, 43])
    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 num_outs,
                 start_level=0,
                 end_level=-1,
                 add_extra_convs=False,
                 extra_convs_on_inputs=True,
                 relu_before_extra_convs=False,
                 no_norm_on_lateral=False,
                 conv_cfg=None,
                 norm_cfg=None,
                 act_cfg=None,
                 upsample_cfg=dict(mode='nearest'),
                 attention_type='fusion',
                 reduction_ratio=16,
                 kernel_size=7,
                 no_channel=False,
                 no_spatial=False,
                 stacking=1,
                 residual=False,
                 map_repeated=1,
                 map_residual=False,
                 fusion_types=('channel_add', 'channel_mul'),
                 weight_type=False,
                 repeated_layer=1,
                 add_fpn=[],
                 down_sharing=False,
                 attention_sharing=False,
                 out_sharing=False,
                 viz=False):
        super(Attention, self).__init__()
        assert isinstance(in_channels, list)
        assert attention_type in ['fusion', 'context', 'augmented', 'self', 'context_weight']
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.add_fpn = add_fpn
        self.num_ins = len(in_channels)
        self.num_outs = num_outs
        self.relu_before_extra_convs = relu_before_extra_convs
        self.attention_type = attention_type
        self.no_norm_on_lateral = no_norm_on_lateral
        self.fp16_enabled = False
        self.upsample_cfg = upsample_cfg.copy()
        self.repeated_layer = repeated_layer
        self.residual = residual
        self.down_sharing = down_sharing
        self.attention_sharing = attention_sharing
        self.out_sharing = out_sharing
        self.viz = viz
        if end_level == -1:
            self.backbone_end_level = self.num_ins
            assert num_outs >= self.num_ins - start_level
        else:
            # if end_level < inputs, no extra level is allowed
            self.backbone_end_level = end_level
            assert end_level <= len(in_channels)
            assert num_outs == end_level - start_level
        self.start_level = start_level
        self.end_level = end_level

        self.fusion_attentions = nn.ModuleList()
        self.downsample_convs = nn.ModuleList()
        self.repeated_convs = nn.ModuleList()

        for r in range(self.repeated_layer):
            if not self.down_sharing or r == 0:
                self.downsample_convs.append(nn.ModuleList())

            if not self.out_sharing or r == 0:
                self.repeated_convs.append(nn.ModuleList())

            if not self.attention_sharing or r == 0:
                self.fusion_attentions.append(nn.ModuleList())
                for i in range(self.start_level, self.backbone_end_level):
                    if attention_type == 'fusion':
                        self.fusion_attentions[-1].append(
                            FusionAttention(
                                gate_channels=out_channels * (self.num_ins - start_level),
                                levels=self.num_ins - start_level,
                                reduction_ratio=reduction_ratio,
                                kernel_size=kernel_size,
                                no_channel=no_channel,
                                no_spatial=no_spatial,
                                stacking=stacking,
                                residual=residual,
                                map_repeated=map_repeated,
                                map_residual=map_residual
                            )
                        )
                    elif attention_type == 'context':
                        self.fusion_attentions[-1].append(
                            ContextBlock(inplanes=out_channels * (self.num_ins - start_level),
                                         levels=self.num_ins - start_level,
                                         repeated=map_repeated,
                                         residual=map_residual,
                                         fusion_types=fusion_types,
                                         weight_type=weight_type,
                                         viz=False)
                        )
                    elif attention_type == 'context_weight':
                        self.fusion_attentions[-1].append(
                            ContextWeightBlcok(inplanes=out_channels * (self.num_ins - start_level),
                                               levels=self.num_ins - start_level,
                                               weight_type=weight_type,
                                               viz=False)
                        )
                    elif attention_type == 'augmented':
                        self.fusion_attentions[-1].append(
                            AugmentedAttention(levels=self.num_ins - start_level,
                                               in_channels=out_channels * (self.num_ins - start_level),
                                               out_channels=out_channels,
                                               map_repeated=map_repeated)
                        )
                    elif attention_type == 'self':
                        self.fusion_attentions[-1].append(
                            SelfAttention(levels=self.num_ins - start_level,
                                          in_channels=out_channels * (self.num_ins - start_level),
                                          out_channels=out_channels)
                        )

            for i in range(self.start_level, self.backbone_end_level):
                d_conv = nn.ModuleList()
                if r != self.repeated_layer - 1 and (not self.out_sharing or r == 0):
                    self.repeated_convs[-1].append(ConvModule(
                        out_channels,
                        out_channels,
                        3,
                        padding=1,
                        conv_cfg=conv_cfg,
                        norm_cfg=norm_cfg,
                        act_cfg=act_cfg,
                        inplace=False))
                for j in range(self.start_level, self.backbone_end_level):
                    temp = []
                    for _ in range(i - j):
                        temp.append(ConvModule(
                            out_channels,
                            out_channels,
                            3,
                            stride=2,
                            padding=1,
                            inplace=False))
                    d_conv.append(nn.Sequential(*temp))
                if not self.down_sharing or r == 0:
                    self.downsample_convs[-1].append(d_conv)

        self.add_extra_convs = add_extra_convs
        assert isinstance(add_extra_convs, (str, bool))
        if isinstance(add_extra_convs, str):
            # Extra_convs_source choices: 'on_input', 'on_lateral', 'on_output'
            assert add_extra_convs in ('on_input', 'on_lateral', 'on_output')
        elif add_extra_convs:  # True
            if extra_convs_on_inputs:
                # TODO: deprecate `extra_convs_on_inputs`
                warnings.simplefilter('once')
                warnings.warn(
                    '"extra_convs_on_inputs" will be deprecated in v2.9.0,'
                    'Please use "add_extra_convs"', DeprecationWarning)
                self.add_extra_convs = 'on_input'
            else:
                self.add_extra_convs = 'on_output'

        self.lateral_convs = nn.ModuleList()
        self.fpn_convs = nn.ModuleList()

        for i in range(self.start_level, self.backbone_end_level):
            l_conv = ConvModule(
                in_channels[i],
                out_channels,
                1,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg if not self.no_norm_on_lateral else None,
                act_cfg=act_cfg,
                inplace=False)
            self.lateral_convs.append(l_conv)

            if not self.out_sharing:
                fpn_conv = ConvModule(
                    out_channels,
                    out_channels,
                    3,
                    padding=1,
                    conv_cfg=conv_cfg,
                    norm_cfg=norm_cfg,
                    act_cfg=act_cfg,
                    inplace=False)
                self.fpn_convs.append(fpn_conv)
            else:
                self.fpn_convs.append(nn.ModuleList())


        # add extra conv layers (e.g., RetinaNet)
        extra_levels = num_outs - self.backbone_end_level + self.start_level
        if self.add_extra_convs and extra_levels >= 1:
            for i in range(extra_levels):
                if i == 0 and self.add_extra_convs == 'on_input':
                    in_channels = self.in_channels[self.backbone_end_level - 1]
                else:
                    in_channels = out_channels
                extra_fpn_conv = ConvModule(
                    in_channels,
                    out_channels,
                    3,
                    stride=2,
                    padding=1,
                    conv_cfg=conv_cfg,
                    norm_cfg=norm_cfg,
                    act_cfg=act_cfg,
                    inplace=False)
                self.fpn_convs.append(extra_fpn_conv)

    # default init_weights for conv(msra) and norm in ConvModule
    def init_weights(self):
        """Initialize the weights of FPN module."""
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                xavier_init(m, distribution='uniform')

    @auto_fp16()
    def forward(self, inputs):
        """Forward function."""
        assert len(inputs) == len(self.in_channels)

        # build laterals
        laterals = [
            lateral_conv(inputs[i + self.start_level])
            for i, lateral_conv in enumerate(self.lateral_convs)
        ]

        if self.viz:
            for lateral in laterals:
                fig, axarr = plt.subplots(3, 3)
                for idx in range(9):
                    axarr[idx // 3][idx % 3].imshow(lateral.squeeze()[idx].squeeze().cpu())
                plt.show()

        if 'before' in self.add_fpn:
            # build top-down path
            used_backbone_levels = len(laterals)
            for i in range(used_backbone_levels - 1, 0, -1):
                # In some cases, fixing `scale factor` (e.g. 2) is preferred, but
                #  it cannot co-exist with `size` in `F.interpolate`.
                if 'scale_factor' in self.upsample_cfg:
                    laterals[i - 1] += F.interpolate(laterals[i],
                                                     **self.upsample_cfg)
                else:
                    prev_shape = laterals[i - 1].shape[2:]
                    laterals[i - 1] += F.interpolate(
                        laterals[i], size=prev_shape, **self.upsample_cfg)

        # build top-down path
        used_backbone_levels = len(laterals)
        for l in range(self.repeated_layer):
            temps = []
            for i in range(used_backbone_levels):
                shape = laterals[i].shape[2:]
                samples = []
                down_l = l
                attention_l = l
                out_l = l
                if self.down_sharing:
                    down_l = 0
                if self.attention_sharing:
                    attention_l = 0
                if self.out_sharing:
                    out_l = 0
                samples.extend([self.downsample_convs[down_l][i][j](laterals[j]) for j in
                                range(i)])
                samples.append(laterals[i])
                samples.extend([F.interpolate(laterals[j], size=shape, **self.upsample_cfg) for j in
                                range(i + 1, used_backbone_levels)])
                if self.residual:
                    temps.append(self.fusion_attentions[attention_l][i](samples) + laterals[i])
                else:
                    temps.append(self.fusion_attentions[attention_l][i](samples))
            if l != self.repeated_layer - 1:
                laterals = [
                    self.repeated_convs[out_l][i](temps[i]) for i in range(used_backbone_levels)
                ]
            else:
                laterals = [temps[j] for j in range(used_backbone_levels)]

        if 'after' in self.add_fpn:
            # build top-down path
            used_backbone_levels = len(laterals)
            for i in range(used_backbone_levels - 1, 0, -1):
                # In some cases, fixing `scale factor` (e.g. 2) is preferred, but
                #  it cannot co-exist with `size` in `F.interpolate`.
                if 'scale_factor' in self.upsample_cfg:
                    laterals[i - 1] += F.interpolate(laterals[i],
                                                     **self.upsample_cfg)
                else:
                    prev_shape = laterals[i - 1].shape[2:]
                    laterals[i - 1] += F.interpolate(
                        laterals[i], size=prev_shape, **self.upsample_cfg)

        if self.viz:
            for lateral in laterals:
                fig, axarr = plt.subplots(3, 3)
                for idx in range(9):
                    axarr[idx // 3][idx % 3].imshow(lateral.squeeze()[idx].squeeze().cpu())
                plt.show()
        # build outputs+
        # part 1: from original levels
        if self.out_sharing:
            outs = [
                self.repeated_convs[0][i](laterals[i]) for i in range(used_backbone_levels)
            ]
        else:
            outs = [
                self.fpn_convs[i](laterals[i]) for i in range(used_backbone_levels)
            ]
        # part 2: add extra levels
        if self.num_outs > len(outs):
            # use max pool to get more levels on top of outputs
            # (e.g., Faster R-CNN, Mask R-CNN)
            if not self.add_extra_convs:
                for i in range(self.num_outs - used_backbone_levels):
                    outs.append(F.max_pool2d(outs[-1], 1, stride=2))
            # add conv layers on top of original feature maps (RetinaNet)
            else:
                if self.add_extra_convs == 'on_input':
                    extra_source = inputs[self.backbone_end_level - 1]
                elif self.add_extra_convs == 'on_lateral':
                    extra_source = laterals[-1]
                elif self.add_extra_convs == 'on_output':
                    extra_source = outs[-1]
                else:
                    raise NotImplementedError
                outs.append(self.fpn_convs[used_backbone_levels](extra_source))
                for i in range(used_backbone_levels + 1, self.num_outs):
                    if self.relu_before_extra_convs:
                        outs.append(self.fpn_convs[i](F.relu(outs[-1])))
                    else:
                        outs.append(self.fpn_convs[i](outs[-1]))
        return tuple(outs)
