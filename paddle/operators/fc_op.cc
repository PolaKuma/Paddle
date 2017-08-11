/* Copyright (c) 2016 PaddlePaddle Authors. All Rights Reserve.

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License. */

#include "paddle/operators/net_op.h"

#include "paddle/framework/eigen.h"
#include "paddle/framework/op_registry.h"

namespace paddle {
namespace operators {

using OpRegistry = framework::OpRegistry;

class FullyConnectedOp : public NetOp {
 public:
  void Init() override {
    AddOp(OpRegistry::CreateOp("mul",
                               {
                                   Input("X"), Input("W"),
                               },
                               {Output("before_act")}, {}));
    auto b = Input("b");
    if (b != framework::kEmptyVarName) {
      AddOp(OpRegistry::CreateOp("rowwise_add",
                                 {Output("before_act"), Input("b")},
                                 {Output("before_act")}, {}));
    }

    auto activation = GetAttr<std::string>("activation");
    AddOp(OpRegistry::CreateOp(activation, {Output("before_act")},
                               {Output("Y")}, {}));
    CompleteAddOp(false);
  }
};

class FullyConnectedOpMaker : public framework::OpProtoAndCheckerMaker {
 public:
  FullyConnectedOpMaker(framework::OpProto *proto,
                        framework::OpAttrChecker *op_checker)
      : OpProtoAndCheckerMaker(proto, op_checker) {
    AddInput("X", "the input of fc operator");
    AddInput("W", "the weight of fc operator");
    AddInput("b", "the bias of fc operator");

    AddOutput("Y", "the output of fc operator");
    AddOutput("before_act", "the before activation output of fc operator")
        .SetTemporary();
    AddAttr<std::string>("activation", "The activation key for fc layer")
        .SetDefault("sigmoid")
        .InEnum({"sigmoid", "softmax"});

    //! TODO(yuyang18): Complete comment;
    AddComment("FullyConnected Operator");
  }
};
}  // namespace operators
}  // namespace paddle

USE_OP(mul);
USE_OP(rowwise_add);
USE_OP(sigmoid);
USE_OP(softmax);

namespace ops = paddle::operators;
REGISTER_OP_WITHOUT_GRADIENT(fc, ops::FullyConnectedOp,
                             ops::FullyConnectedOpMaker);
