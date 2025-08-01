# Copyright 2023-2024 SGLang Team
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Fused operators for activation layers."""

import logging
import math
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import PretrainedConfig

from sglang.srt.custom_op import CustomOp
from sglang.srt.distributed import (
    divide,
    get_tensor_model_parallel_rank,
    get_tensor_model_parallel_world_size,
)
from sglang.srt.layers.quantization.base_config import QuantizationConfig
from sglang.srt.utils import (
    cpu_has_amx_support,
    is_cpu,
    is_cuda,
    is_hip,
    is_npu,
    set_weight_attrs,
)
from sglang.utils import resolve_obj_by_qualname

_is_cuda = is_cuda()
_is_npu = is_npu()
_is_cpu_amx_available = cpu_has_amx_support()
_is_cpu = is_cpu()
_is_hip = is_hip()

if _is_cuda:
    from sgl_kernel import gelu_and_mul, gelu_tanh_and_mul, silu_and_mul
elif _is_hip:
    from sgl_kernel import gelu_and_mul, gelu_quick, gelu_tanh_and_mul, silu_and_mul

if is_npu():
    import torch_npu

logger = logging.getLogger(__name__)


class SiluAndMul(CustomOp):
    def forward_native(self, x: torch.Tensor) -> torch.Tensor:
        d = x.shape[-1] // 2
        return F.silu(x[..., :d]) * x[..., d:]

    def forward_cuda(self, x: torch.Tensor) -> torch.Tensor:
        d = x.shape[-1] // 2
        output_shape = x.shape[:-1] + (d,)
        out = torch.empty(output_shape, dtype=x.dtype, device=x.device)
        silu_and_mul(x, out)
        return out

    def forward_cpu(self, x: torch.Tensor) -> torch.Tensor:
        if _is_cpu_amx_available:
            d = x.shape[-1] // 2
            output_shape = x.shape[:-1] + (d,)
            out = torch.ops.sgl_kernel.silu_and_mul_cpu(x)
            return out
        else:
            return self.forward_native(x)

    def forward_npu(self, x: torch.Tensor) -> torch.Tensor:
        out = torch_npu.npu_swiglu(x)
        return out


class GeluAndMul(CustomOp):
    def __init__(self, approximate="tanh"):
        super().__init__()
        self.approximate = approximate

    def forward_native(self, x: torch.Tensor) -> torch.Tensor:
        d = x.shape[-1] // 2
        return F.gelu(x[..., :d], approximate=self.approximate) * x[..., d:]

    def forward_cuda(self, x: torch.Tensor) -> torch.Tensor:
        d = x.shape[-1] // 2
        output_shape = x.shape[:-1] + (d,)
        out = torch.empty(output_shape, dtype=x.dtype, device=x.device)
        if self.approximate == "tanh":
            gelu_tanh_and_mul(x, out)
        elif self.approximate == "none":
            gelu_and_mul(x, out)
        else:
            raise RuntimeError("GeluAndMul only support tanh or none")
        return out


class NewGELU(CustomOp):
    def forward_native(self, x: torch.Tensor) -> torch.Tensor:
        c = math.sqrt(2.0 / math.pi)
        return 0.5 * x * (1.0 + torch.tanh(c * (x + 0.044715 * torch.pow(x, 3.0))))

    def forward_cuda(self, x: torch.Tensor) -> torch.Tensor:
        # TODO: Implement the CUDA kernel for NewGELU in sgl-kernel
        return self.forward_native(x)


class ReLU2(nn.Module):
    """
    Applies the squared Rectified Linear Unit function.
    y = max(0, x)^2
    """

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.relu(x)
        return x * x


class QuickGELU(CustomOp):
    def forward_native(self, x: torch.Tensor) -> torch.Tensor:
        return x * torch.sigmoid(1.702 * x)

    def forward_cuda(self, x: torch.Tensor) -> torch.Tensor:
        return self.forward_native(x)

    def forward_hip(self, x: torch.Tensor) -> torch.Tensor:
        out = torch.empty(x.shape, dtype=x.dtype, device=x.device)
        gelu_quick(x, out)
        return out


class ScaledActivation(nn.Module):
    """An activation function with post-scale parameters.

    This is used for some quantization methods like AWQ.
    """

    def __init__(
        self,
        act_module: nn.Module,
        intermediate_size: int,
        input_is_parallel: bool = True,
        params_dtype: Optional[torch.dtype] = None,
    ):
        super().__init__()
        self.act = act_module
        self.input_is_parallel = input_is_parallel
        if input_is_parallel:
            tp_size = get_tensor_model_parallel_world_size()
            intermediate_size_per_partition = divide(intermediate_size, tp_size)
        else:
            intermediate_size_per_partition = intermediate_size
        if params_dtype is None:
            params_dtype = torch.get_default_dtype()
        self.scales = nn.Parameter(
            torch.empty(intermediate_size_per_partition, dtype=params_dtype)
        )
        set_weight_attrs(self.scales, {"weight_loader": self.weight_loader})

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.act(x) / self.scales

    def weight_loader(self, param: nn.Parameter, loaded_weight: torch.Tensor):
        param_data = param.data
        if self.input_is_parallel:
            tp_rank = get_tensor_model_parallel_rank()
            shard_size = param_data.shape[0]
            start_idx = tp_rank * shard_size
            loaded_weight = loaded_weight.narrow(0, start_idx, shard_size)
        assert param_data.shape == loaded_weight.shape
        param_data.copy_(loaded_weight)


_ACTIVATION_REGISTRY = {
    "gelu": nn.GELU(),
    "gelu_pytorch_tanh": nn.GELU(approximate="tanh"),
    "gelu_new": NewGELU(),
    "relu2": ReLU2(),
}


def get_act_fn(
    act_fn_name: str,
    quant_config: Optional[QuantizationConfig] = None,
    intermediate_size: Optional[int] = None,
    input_is_parallel: bool = True,
    params_dtype: Optional[torch.dtype] = None,
) -> nn.Module:
    """Get an activation function by name."""
    act_fn_name = act_fn_name.lower()
    if act_fn_name not in _ACTIVATION_REGISTRY:
        raise ValueError(f"Activation function {act_fn_name!r} is not supported.")

    act_fn = _ACTIVATION_REGISTRY[act_fn_name]
    if quant_config is not None and act_fn_name in quant_config.get_scaled_act_names():
        if intermediate_size is None:
            raise ValueError(
                "intermediate_size must be specified for scaled "
                "activation functions."
            )
        return ScaledActivation(
            act_fn, intermediate_size, input_is_parallel, params_dtype
        )
    return act_fn


def get_cross_encoder_activation_function(config: PretrainedConfig):
    if (
        hasattr(config, "sbert_ce_default_activation_function")
        and config.sbert_ce_default_activation_function is not None
    ):

        function_name = config.sbert_ce_default_activation_function
        assert function_name.startswith("torch.nn.modules."), (
            "Loading of activation functions is restricted to "
            "torch.nn.modules for security reasons"
        )
        return resolve_obj_by_qualname(function_name)()
    else:
        # adapt bge-reranker
        return nn.Identity()


if not (_is_cuda or _is_npu or (_is_cpu and _is_cpu_amx_available) or _is_hip):
    logger.info(
        "sgl-kernel is not available on Non-NV, Non-AMD platforms or Non-AMX CPUs. Fallback to other kernel libraries."
    )
    from vllm.model_executor.layers.activation import GeluAndMul, SiluAndMul
