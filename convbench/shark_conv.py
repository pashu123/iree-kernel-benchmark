import os
from tqdm import tqdm
from multiprocessing import Pool, cpu_count, Manager
import logging
import itertools
from pathlib import Path
import csv
import argparse
import sys
from utils import *
from conv_utils import *
from problems import get_conv_configs


def compile_conv(tag, config, kernel_dir, vmfb_dir):
    mlir_file, vmfb_file = compile_conv_config(config, kernel_dir, vmfb_dir)
    return (tag, config, mlir_file, vmfb_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Config file updater.")
    parser.add_argument(
        "--log-level",
        default="ERROR",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        type=str.upper,
        help="Set the logging level",
    )
    parser.add_argument(
        "--device",
        help="The IREE device to execute benchmarks on",
        type=str,
        default="hip",
    )
    parser.add_argument(
        "--roofline",
        help="Comma seperated csv file list to generate roofline plot with",
        default=None,
    )
    parser.add_argument("--plot", help="location to save plot", default=None)
    parser.add_argument(
        "--batch", help="roofline on certain batch", type=int, default=None
    )
    parser.add_argument("--dtype", help="roofline on certain dtype", default=None)
    parser.add_argument("--model", help="roofline on certain model", default=None)

    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)

    if args.roofline:
        roofline(args.roofline, args.plot, args.batch, args.dtype, args.model)
        sys.exit()

    configs = get_conv_configs()
    print(f"Generated {len(configs)} conv configs.")

    num_cpus = max(1, cpu_count() - 20)
    print(f"Using {num_cpus} CPUs for parallel processing.")

    manager = Manager()
    vmfb_dict = manager.dict()

    repo_root = Path(__file__).parent.parent
    kernel_dir = repo_root / "conv" / "mlir"
    vmfb_dir = repo_root / "conv" / "vmfb"
    kernel_dir.mkdir(parents=True, exist_ok=True)
    vmfb_dir.mkdir(parents=True, exist_ok=True)
    device = args.device

    compile_args = itertools.starmap(
        lambda tag, config: (tag, config, kernel_dir, vmfb_dir), configs
    )
    with Pool(num_cpus) as pool:
        compilation_results = list(tqdm(pool.starmap(compile_conv, list(compile_args))))

    error_count = 0
    for tag, config, mlir_file, vmfb_file in compilation_results:
        if vmfb_file:
            vmfb_dict[vmfb_file] = (tag, config)
        else:
            error_count += 1
    print(
        f"{len(configs) - error_count} Success, {error_count} Failed out of {len(configs)} configs"
    )

    print("Compilation process completed.")

    results = []
    index = 0
    output_csv = "results/iree_conv.csv"
    csv_dir = os.path.dirname(output_csv)
    if not os.path.exists(csv_dir):
        os.makedirs(csv_dir)

    for vmfb_filename, value in vmfb_dict.items():
        tag, config = value
        name = config.get_name()

        image_shape = config.get_img_shape()
        filter_shape = config.get_kernel_shape()

        exec_args = [
            "iree-benchmark-module",
            f"--device={device}",
            "--device_allocator=caching",
            f"--module={vmfb_filename}",
            "--function=main",
            f"--input={image_shape}",
            f"--input={filter_shape}",
            "--benchmark_repetitions=3",
        ]

        # iree benchmark kernels
        ret_value, cmd_out, cmd_stderr = run_iree_command(exec_args)
        ok = ret_value == 0
        benchmark_gemm_mean_time_ms = bench_summary_process(ret_value, cmd_out)
        benchmark_gemm_mean_time_us = benchmark_gemm_mean_time_ms * 1000

        flops = config.get_flops()
        byte_count = config.get_byte_count()

        arithmetic_intensity = flops / byte_count
        tflops_per_second = (flops / 1e12) / (benchmark_gemm_mean_time_us / 1e6)

        results.append(
            (
                index,
                tag,
                name,
                config.N,
                config.H,
                config.W,
                config.C,
                config.P,
                config.Q,
                config.F,
                config.S,
                config.input_dtype,
                config.output_dtype,
                round(benchmark_gemm_mean_time_us, 4),
                round(arithmetic_intensity, 4),
                round(tflops_per_second, 4),
                ok,
            )
        )
        index += 1

    fieldnames = [
        "index",
        "tag",
        "name",
        "B",
        "H",
        "W",
        "C",
        "P",
        "Q",
        "F",
        "S",
        "input_dtype",
        "output_dtype",
        "mean_microseconds",
        "arithmetic_intensity",
        "tflops",
        "ok",
    ]

    write_results_to_csv(results, output_csv, fieldnames)
    print(f"Results written to {output_csv}")
