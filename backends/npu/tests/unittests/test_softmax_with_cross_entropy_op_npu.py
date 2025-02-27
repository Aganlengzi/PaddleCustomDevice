#  Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import unittest

import numpy as np
import paddle
import paddle.fluid as fluid
from tests.op_test import OpTest

paddle.enable_static()
SEED = 2021


def stable_softmax(x):
    """Compute the softmax of vector x in a numerically stable way."""
    # clip to shiftx, otherwise, when calc loss with
    # log(exp(shiftx)), may get log(0)=INF
    shiftx = (x - np.max(x)).clip(-64.0)
    exps = np.exp(shiftx)
    return exps / np.sum(exps)


def cross_entropy(softmax, label, soft_label, axis, ignore_index=-1):
    if soft_label:
        return (-label * np.log(softmax)).sum(axis=axis, keepdims=True)
    shape = softmax.shape
    axis %= len(shape)
    n = int(np.prod(shape[:axis]))
    axis_dim = shape[axis]
    remain = int(np.prod(shape[axis + 1 :]))
    softmax_reshape = softmax.reshape((n, axis_dim, remain))
    label_reshape = label.reshape((n, 1, remain))
    result = np.zeros_like(label_reshape, dtype=softmax.dtype)
    for i in range(n):
        for j in range(remain):
            lbl = label_reshape[i, 0, j]
            if lbl != ignore_index:
                result[i, 0, j] -= np.log(softmax_reshape[i, lbl, j])
    return result.reshape(label.shape)


class TestSoftmaxWithCrossEntropyOp(OpTest):
    def set_npu(self):
        self.__class__.use_custom_device = True

    def init_dtype(self):
        self.dtype = np.float32

    def initParams(self):
        self.set_npu()
        self.op_type = "softmax_with_cross_entropy"
        self.numeric_stable_mode = False
        self.place = paddle.CustomPlace("npu", 0)
        self.soft_label = False
        self.init_dtype()
        self.axis = -1
        self.ignore_index = -1
        self.shape = [41, 37]
        np.random.seed(SEED)

    def setUp(self):
        self.initParams()

        logits = getattr(
            self, "logits", np.random.uniform(0.1, 1.0, self.shape).astype(self.dtype)
        )
        softmax = np.apply_along_axis(stable_softmax, self.axis, logits)

        if self.soft_label:
            labels = np.random.uniform(0.1, 1.0, self.shape).astype(self.dtype)
            labels /= np.sum(labels, axis=self.axis, keepdims=True)
        else:
            axis_dim = self.shape[self.axis]
            self.shape[self.axis] = 1
            labels = np.random.randint(0, axis_dim, self.shape, dtype="int64")

        loss = cross_entropy(
            softmax, labels, self.soft_label, self.axis, self.ignore_index
        )

        one_hot_label = np.eye(axis_dim)[labels.reshape(-1)]

        self.inputs = {"Logits": logits, "Label": labels}
        self.outputs = {
            "Softmax": softmax.astype(self.dtype),
            "Loss": loss.astype(self.dtype),
        }
        self.attrs = {
            "numeric_stable_mode": self.numeric_stable_mode,
            "soft_label": self.soft_label,
            "ignore_index": self.ignore_index,
        }

        if self.axis != -1:
            self.attrs["axis"] = self.axis

    def test_check_output(self):
        self.check_output_with_place(self.place)

    def test_check_grad(self):
        # fp32 has low precision, cpu and npu both need to relax the max_relative_error if using fp32
        self.check_grad_with_place(
            self.place,
            ["Logits"],
            "Loss",
            numeric_grad_delta=0.001,
            max_relative_error=0.5,
        )


class TestPowNet(unittest.TestCase):
    def _test(self, run_npu=True):
        main_prog = paddle.static.Program()
        startup_prog = paddle.static.Program()
        main_prog.random_seed = SEED
        startup_prog.random_seed = SEED
        np.random.seed(SEED)

        a_np = np.random.random(size=(32, 32)).astype("float32")
        b_np = np.random.random(size=(32, 32)).astype("float32")
        label_np = np.random.randint(2, size=(32, 1)).astype("int64")

        with paddle.static.program_guard(main_prog, startup_prog):
            a = paddle.static.data(name="a", shape=[32, 32], dtype="float32")
            b = paddle.static.data(name="b", shape=[32, 32], dtype="float32")
            label = paddle.static.data(name="label", shape=[32, 1], dtype="int64")

            sum = paddle.add(a, b)
            z = paddle.pow(sum, 2.0)

            fc_1 = fluid.layers.fc(input=z, size=128)
            prediction = fluid.layers.fc(input=fc_1, size=2)

            cost = fluid.layers.softmax_with_cross_entropy(prediction, label)
            loss = paddle.mean(cost)
            sgd = fluid.optimizer.SGD(learning_rate=0.01)
            sgd.minimize(loss)

        if run_npu:
            place = paddle.CustomPlace("npu", 0)
        else:
            place = paddle.CPUPlace()

        exe = paddle.static.Executor(place)
        exe.run(startup_prog)

        print("Start run on {}".format(place))
        for epoch in range(100):

            pred_res, loss_res = exe.run(
                main_prog,
                feed={"a": a_np, "b": b_np, "label": label_np},
                fetch_list=[prediction, loss],
            )
            if epoch % 10 == 0:
                print(
                    "Epoch {} | Prediction[0]: {}, Loss: {}".format(
                        epoch, pred_res[0], loss_res
                    )
                )

        return pred_res, loss_res

    def test_npu(self):
        cpu_pred, cpu_loss = self._test(False)
        npu_pred, npu_loss = self._test(True)

        self.assertTrue(np.allclose(npu_pred, cpu_pred))
        self.assertTrue(np.allclose(npu_loss, cpu_loss))


if __name__ == "__main__":
    unittest.main()
