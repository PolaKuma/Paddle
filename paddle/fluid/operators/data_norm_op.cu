/* Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License. */

#include <memory>
#include <string>

#include "paddle/fluid/framework/data_layout.h"
#include "paddle/fluid/operators/data_norm_op.h"
#include "paddle/phi/backends/gpu/gpu_primitives.h"
#if defined(PADDLE_WITH_NCCL) || defined(PADDLE_WITH_RCCL)
#include "paddle/common/flags.h"
#include "paddle/fluid/platform/device/gpu/nccl_helper.h"
#include "paddle/phi/core/distributed/comm_context_manager.h"
#include "paddle/phi/core/distributed/nccl_comm_context.h"
#include "paddle/phi/core/platform/collective_helper.h"
#endif

namespace paddle {
namespace operators {

using DataLayout = phi::DataLayout;
using phi::PADDLE_CUDA_NUM_THREADS;

inline int GET_BLOCKS(const int N) {
  return (N + PADDLE_CUDA_NUM_THREADS - 1) / PADDLE_CUDA_NUM_THREADS;
}

template <typename T>
__global__ void KernelDataNormFF(
    int N, int C, const T *x, T *y, const T *mean, const T *scale) {
  CUDA_KERNEL_LOOP(i, N * C) {
    int col = i % C;
    y[i] = (x[i] - mean[col]) * scale[col];
  }
}

template <typename T>
__global__ void KernelMeanScale(int C,
                                const T *batch_size,
                                const T *batch_sum,
                                const T *batch_square_sum,
                                T *mean,
                                T *scale) {
  CUDA_KERNEL_LOOP(i, C) {
    mean[i] = batch_sum[i] / batch_size[i];
    scale[i] = sqrt(batch_size[i] / batch_square_sum[i]);
  }
}

template <typename T>
__global__ void KernelDataNormBP(
    int N, int C, const T *y_grad, const T *scale, T *x_grad) {
  CUDA_KERNEL_LOOP(i, N * C) { x_grad[i] = y_grad[i] * scale[i % C]; }
}

template <typename T>
__global__ void KernelDataNormBPStat(int N,
                                     int C,
                                     const T *x_val,
                                     const T *means,
                                     const float squared_sum_epsilon,
                                     T *batch_size,
                                     T *batch_sum,
                                     T *batch_square_sum) {
  CUDA_KERNEL_LOOP(i, C) {
    T val_sum = 0;
    T square_sum = 0;
    for (int j = 0; j < N; j++) {
      val_sum += x_val[j * C + i];
      square_sum +=
          (x_val[j * C + i] - means[i]) * (x_val[j * C + i] - means[i]);
    }
    batch_size[i] = 1;
    batch_sum[i] = val_sum / N;
    batch_square_sum[i] = square_sum / N + squared_sum_epsilon;
  }
}

template <typename T>
__global__ void KernelUpdateParam(int C,
                                  const T *d_batch_size,
                                  const T *d_batch_sum,
                                  const T *d_batch_square_sum,
                                  T *batch_size,
                                  T *batch_sum,
                                  T *batch_square_sum,
                                  const float decay_rate) {
  CUDA_KERNEL_LOOP(i, C) {
    batch_size[i] = batch_size[i] * decay_rate + d_batch_size[i];
    batch_sum[i] = batch_sum[i] * decay_rate + d_batch_sum[i];
    batch_square_sum[i] =
        batch_square_sum[i] * decay_rate + d_batch_square_sum[i];
  }
}

template <typename T>
class DataNormKernel<T, phi::GPUContext> : public framework::OpKernel<T> {
 public:
  void Compute(const framework::ExecutionContext &ctx) const override {
    const auto *x = ctx.Input<phi::DenseTensor>("X");
    const auto &x_dims = x->dims();
    // Align with CPU version, but should we add this restriction?
    PADDLE_ENFORCE_EQ(
        x_dims.size(),
        2,
        common::errors::PreconditionNotMet("The Input dim size should be 2"));
    const int N = x_dims[0];
    const int C = x_dims[1];

    PADDLE_ENFORCE_LT(0,
                      N,
                      common::errors::InvalidArgument(
                          "The dims of Input(X) should be greater than 0."));
    PADDLE_ENFORCE_LT(0,
                      C,
                      common::errors::InvalidArgument(
                          "The dims of Input(X) should be greater than 0."));

    const T *batch_size_in =
        ctx.Input<phi::DenseTensor>("BatchSize")->data<T>();
    const T *batch_sum_in = ctx.Input<phi::DenseTensor>("BatchSum")->data<T>();
    const T *batch_square_sum_in =
        ctx.Input<phi::DenseTensor>("BatchSquareSum")->data<T>();
    auto *x_data = x->data<T>();

    // alloc memory
    T *y_data =
        ctx.Output<phi::DenseTensor>("Y")->mutable_data<T>(ctx.GetPlace());
    T *mean_out_data =
        ctx.Output<phi::DenseTensor>("Means")->mutable_data<T>(ctx.GetPlace());
    T *scale_out_data =
        ctx.Output<phi::DenseTensor>("Scales")->mutable_data<T>(ctx.GetPlace());

    auto stream = ctx.template device_context<phi::GPUContext>().stream();

    KernelMeanScale<<<GET_BLOCKS(C), PADDLE_CUDA_NUM_THREADS, 0, stream>>>(
        C,
        batch_size_in,
        batch_sum_in,
        batch_square_sum_in,
        mean_out_data,
        scale_out_data);
    KernelDataNormFF<<<GET_BLOCKS(C * N), PADDLE_CUDA_NUM_THREADS, 0, stream>>>(
        N, C, x_data, y_data, mean_out_data, scale_out_data);
  }
};

template <typename T>
class DataNormGradKernel<T, phi::GPUContext> : public framework::OpKernel<T> {
 public:
  void Compute(const framework::ExecutionContext &ctx) const override {
    const auto *x = ctx.Input<phi::DenseTensor>("X");
    const auto *d_y = ctx.Input<phi::DenseTensor>(framework::GradVarName("Y"));
    const auto *scales = ctx.Input<phi::DenseTensor>("Scales");
    const auto *means = ctx.Input<phi::DenseTensor>("Means");
    const float epsilon = ctx.Attr<float>("epsilon");
    const float dr = ctx.Attr<float>("summary_decay_rate");
    const bool need_sync_stats = ctx.Attr<bool>("sync_stats");

    const auto &x_dims = x->dims();
    // Align with CPU version, but should we add this restriction?
    PADDLE_ENFORCE_EQ(
        x_dims.size(),
        2,
        common::errors::PreconditionNotMet("The Input dim size should be 2"));
    const int N = x_dims[0];
    const int C = x_dims[1];

    // init output
    phi::DenseTensor *d_x = nullptr;
    if (ctx.HasOutput(framework::GradVarName("X"))) {
      d_x = ctx.Output<phi::DenseTensor>(framework::GradVarName("X"));
    }
    T *d_batch_size =
        ctx.Output<phi::DenseTensor>(framework::GradVarName("BatchSize"))
            ->mutable_data<T>(ctx.GetPlace());
    T *d_batch_sum =
        ctx.Output<phi::DenseTensor>(framework::GradVarName("BatchSum"))
            ->mutable_data<T>(ctx.GetPlace());
    T *d_batch_square_sum =
        ctx.Output<phi::DenseTensor>(framework::GradVarName("BatchSquareSum"))
            ->mutable_data<T>(ctx.GetPlace());

    auto stream = ctx.template device_context<phi::GPUContext>().stream();
    if (d_x != nullptr) {
      KernelDataNormBP<<<GET_BLOCKS(C * N),
                         PADDLE_CUDA_NUM_THREADS,
                         0,
                         stream>>>(N,
                                   C,
                                   d_y->data<T>(),
                                   scales->data<T>(),
                                   d_x->mutable_data<T>(ctx.GetPlace()));
    }

    KernelDataNormBPStat<<<GET_BLOCKS(C), PADDLE_CUDA_NUM_THREADS, 0, stream>>>(
        N,
        C,
        x->data<T>(),
        means->data<T>(),
        epsilon,
        d_batch_size,
        d_batch_sum,
        d_batch_square_sum);

    if (need_sync_stats) {
#if defined(PADDLE_WITH_NCCL) || defined(PADDLE_WITH_RCCL)
      int rid = 0;
      const auto &comm_context_manager =
          phi::distributed::CommContextManager::GetInstance();
      phi::distributed::NCCLCommContext *comm_ctx = nullptr;
      PADDLE_ENFORCE_EQ(comm_context_manager.Has(std::to_string(rid)),
                        true,
                        common::errors::InvalidArgument(
                            "You choose to use new communication library. "
                            "But ring_id(%d) is "
                            "not found in comm_context_manager.",
                            std::to_string(rid)));
      comm_ctx = static_cast<phi::distributed::NCCLCommContext *>(
          comm_context_manager.Get(std::to_string(rid)));
      PADDLE_ENFORCE_NE(comm_ctx,
                        nullptr,
                        common::errors::Unavailable(
                            "NCCLCommContext is nullptr, collective op should "
                            "has ring_id attr."));

      PADDLE_ENFORCE_GPU_SUCCESS(phi::dynload::ncclAllReduce(
          reinterpret_cast<const void *>(d_batch_size),
          reinterpret_cast<void *>(d_batch_size),
          C,
          phi::ToNCCLDataType(x->dtype()),
          ncclSum,
          comm_ctx->GetNcclComm(),
          stream));
      PADDLE_ENFORCE_GPU_SUCCESS(phi::dynload::ncclAllReduce(
          reinterpret_cast<const void *>(d_batch_sum),
          reinterpret_cast<void *>(d_batch_sum),
          C,
          phi::ToNCCLDataType(x->dtype()),
          ncclSum,
          comm_ctx->GetNcclComm(),
          stream));
      PADDLE_ENFORCE_GPU_SUCCESS(phi::dynload::ncclAllReduce(
          reinterpret_cast<const void *>(d_batch_square_sum),
          reinterpret_cast<void *>(d_batch_square_sum),
          C,
          phi::ToNCCLDataType(x->dtype()),
          ncclSum,
          comm_ctx->GetNcclComm(),
          stream));
      phi::backends::gpu::GpuStreamSync(stream);
#else
      PADDLE_THROW(common::errors::PreconditionNotMet(
          "PaddlePaddle should compile with GPU, and need_sync_stats cannot be "
          "supported on windows now."));
#endif
    }

    T *batch_size_data = ctx.Output<phi::DenseTensor>("BatchSize")
                             ->mutable_data<T>(ctx.GetPlace());
    T *batch_sum_data = ctx.Output<phi::DenseTensor>("BatchSum")
                            ->mutable_data<T>(ctx.GetPlace());
    T *batch_square_sum_data = ctx.Output<phi::DenseTensor>("BatchSquareSum")
                                   ->mutable_data<T>(ctx.GetPlace());
    KernelUpdateParam<<<GET_BLOCKS(C), PADDLE_CUDA_NUM_THREADS, 0, stream>>>(
        C,
        d_batch_size,
        d_batch_sum,
        d_batch_square_sum,
        batch_size_data,
        batch_sum_data,
        batch_square_sum_data,
        dr);
  }
};
}  // namespace operators
}  // namespace paddle

namespace ops = paddle::operators;

PD_REGISTER_STRUCT_KERNEL(
    data_norm, GPU, ALL_LAYOUT, ops::DataNormKernel, float, double) {}
PD_REGISTER_STRUCT_KERNEL(
    data_norm_grad, GPU, ALL_LAYOUT, ops::DataNormGradKernel, float, double) {}
