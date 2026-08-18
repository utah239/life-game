#!/usr/bin/env python3
"""Numba-CUDAのdriver認識・転送・kernel JITをまとめて確認する診断用。"""
import sys
import time

import numpy as np
import numba
from numba import cuda


@cuda.jit
def probe_kernel(values, output):
    index = cuda.grid(1)
    if index >= values.size:
        return
    value = values[index]
    for step in range(64):
        if (index + step) & 1:
            value = value * np.float32(1.00001) + np.float32(0.0001)
        else:
            value = value * np.float32(0.99999) - np.float32(0.0001)
    output[index] = value


def main() -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"Numba: {numba.__version__}")
    print(f"CUDA available: {cuda.is_available()}")
    if not cuda.is_available():
        return 2
    cuda.detect()

    count = 1_000_000
    host_input = np.linspace(0.0, 1.0, count, dtype=np.float32)
    device_input = cuda.to_device(host_input)
    device_output = cuda.device_array_like(device_input)
    threads = 256
    blocks = (count + threads - 1) // threads

    probe_kernel[blocks, threads](device_input, device_output)
    cuda.synchronize()
    started = time.perf_counter()
    repeats = 20
    for _ in range(repeats):
        probe_kernel[blocks, threads](device_input, device_output)
    cuda.synchronize()
    elapsed = time.perf_counter() - started
    output = device_output.copy_to_host()
    if not np.isfinite(output).all():
        raise RuntimeError("GPU output contains a non-finite value")
    updates = count * 64 * repeats
    print(f"Kernel JIT/execution: OK")
    print(f"Elapsed: {elapsed:.3f}s")
    print(f"Branch updates: {updates / elapsed / 1_000_000:.1f} million/s")
    print(f"Checksum: {float(output.sum(dtype=np.float64)):.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
