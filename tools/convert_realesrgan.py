"""One-off: convert Real-ESRGAN 'realesr-general-x4v3.pth' (SRVGGNetCompact) to ONNX
without needing PyTorch. Usage: python tools/convert_realesrgan.py in.pth out.onnx"""
import io
import pickle
import sys
import zipfile

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


class _Storage:
    def __init__(self, raw):
        self.raw = raw


def load_pth(path):
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    root = names[0].split("/")[0]
    pkl = zf.read(f"{root}/data.pkl")

    class U(pickle.Unpickler):
        def find_class(self, module, name):
            if name == "_rebuild_tensor_v2":
                def rebuild(storage, offset, size, stride, *a):
                    arr = np.frombuffer(storage.raw, dtype=np.float32)
                    n = int(np.prod(size)) if len(size) else 1
                    return arr[offset: offset + n].reshape(size).copy()
                return rebuild
            if name.endswith("Storage"):
                return name
            if module == "collections" and name == "OrderedDict":
                import collections
                return collections.OrderedDict
            return super().find_class(module, name)

        def persistent_load(self, pid):
            _, _, key, _, _ = pid
            return _Storage(zf.read(f"{root}/data/{key}"))

    obj = U(io.BytesIO(pkl)).load()
    return obj.get("params", obj.get("params_ema", obj))


def main(src, dst):
    sd = load_pth(src)
    keys = list(sd.keys())
    convs = sorted({int(k.split(".")[1]) for k in keys if k.endswith("weight") and sd[k].ndim == 4})
    nodes, inits = [], []
    cur = "x"
    for n, idx in enumerate(convs):
        w, b = sd[f"body.{idx}.weight"], sd[f"body.{idx}.bias"]
        inits += [numpy_helper.from_array(w, f"w{idx}"), numpy_helper.from_array(b, f"b{idx}")]
        out = f"c{idx}"
        nodes.append(helper.make_node("Conv", [cur, f"w{idx}", f"b{idx}"], [out], pads=[1, 1, 1, 1], kernel_shape=[3, 3]))
        cur = out
        if f"body.{idx + 1}.weight" in sd and sd[f"body.{idx + 1}.weight"].ndim == 1:   # PReLU after it
            slope = sd[f"body.{idx + 1}.weight"].reshape(-1, 1, 1)
            inits.append(numpy_helper.from_array(slope, f"s{idx}"))
            nodes.append(helper.make_node("PRelu", [cur, f"s{idx}"], [f"p{idx}"]))
            cur = f"p{idx}"
    nodes.append(helper.make_node("DepthToSpace", [cur], ["ps"], blocksize=4, mode="CRD"))   # nn.PixelShuffle(4)
    inits.append(numpy_helper.from_array(np.array([1, 1, 4, 4], dtype=np.float32), "scales"))
    inits.append(numpy_helper.from_array(np.array([], dtype=np.float32), "roi"))
    nodes.append(helper.make_node("Resize", ["x", "roi", "scales"], ["up"], mode="nearest"))
    nodes.append(helper.make_node("Add", ["ps", "up"], ["y"]))
    g = helper.make_graph(
        nodes, "srvgg",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, "h", "w"])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 3, "H", "W"])], inits)
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)])
    m.ir_version = 8
    onnx.checker.check_model(m)
    onnx.save(m, dst)
    print("convs:", len(convs), "->", dst)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
