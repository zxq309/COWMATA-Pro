"""Offline numerical worker, limited to two CPUs and one numerical library thread."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def progress_writer(path, *, clock=time.monotonic, interval=0.25):
    """Bound progress I/O while immediately reporting phase changes and completion."""
    from cowmata_tailring.workspace.storage import atomic_json
    previous_phase = None
    last_write = float('-inf')

    def report(done, total, message):
        nonlocal previous_phase, last_write
        now = clock()
        phase = (message, total)
        if phase == previous_phase and done != total and now - last_write < interval:
            return
        atomic_json(Path(path), dict(done=done, total=total, message=message), backup=False)
        previous_phase, last_write = phase, now

    return report


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cowmata_security.worker import authorize_worker
    authorize_worker(Path(__file__).resolve().parents[2], 'behavior')
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ[name] = "1"
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel.GetCurrentProcess()
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetPriorityClass(handle, 0x4000)
        kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        allowed, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel.GetProcessAffinityMask(handle, ctypes.byref(allowed), ctypes.byref(system)):
            raise OSError("Could not inspect algorithm worker CPU affinity")
        bits = [1 << i for i in range(64) if allowed.value & (1 << i)]
        if not kernel.SetProcessAffinityMask(handle, sum(bits[-2:])):
            raise OSError("Could not limit algorithm worker CPU affinity")
    elif hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    from cowmata_tailring.algorithms.analysis import analyze_patterns, load_features
    from cowmata_tailring.algorithms.dataset import scan_dataset
    from cowmata_tailring.algorithms.evidence import build_evidence, infer_features
    from cowmata_tailring.algorithms.registry import read_suite
    from cowmata_tailring.workspace.storage import atomic_json
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    progress = progress_writer(request["progress"])

    action = request["action"]
    if action == 'inspect390':
        from cowmata_tailring.algorithms.inputs import scan_inputs
        result = scan_inputs(request['dataset'], training=request.get('training',False), progress=progress)
    elif action == 'folder_predict393':
        from cowmata_tailring.algorithms.live_prediction import predict_folder
        result = predict_folder(request['folder'], request['model'], request['output'],
                                request['cache'], request['model_home'], progress=progress)
    elif action == 'fusion390':
        from cowmata_tailring.algorithms.decision import build_fusion
        result = build_fusion(request['root'],request['suites'],request['output'],request['cache'],codes=request.get('codes'),selections=request.get('selections'),progress=progress)
    elif action in ('decision_train390','decision_predict390'):
        from cowmata_tailring.algorithms.decision import predict_decision, train_decision
        evidence = json.loads(Path(request['evidence']).read_text(encoding='utf-8'))
        if action == 'decision_train390':
            result = train_decision(evidence,request['ledger'],request['output'],request['algorithm'],request['horizon'],progress=progress)
        else:
            result = predict_decision(evidence,request['model'],request['output'])
    elif action == 'recognize390':
        from cowmata_tailring.algorithms.recognition import recognize
        result = recognize(request['root'],request['suite'],request['code'],request['output'],request['cache'],progress=progress)
    elif action == "predict":
        suite = read_suite(request["suite"])
        feature = load_features(request["record"], request["cache"])
        events = infer_features(suite, feature, [request["code"]])
        result = dict(candidates=events, audit=dict(
            feature_version=feature["feature_version"], observed_seconds=feature["observed_seconds"],
            duration_ms=feature["duration_ms"], gap_count=feature["gap_count"],
            warnings=feature["warnings"], model_version=suite["version"],
            interpretation="模型分数不是概率；候选区间需要人工确认"))
    else:
        if request.get("record"):
            index = dict(records=[request["record"]], fingerprint=request["record"]["asset_id"], issues=[])
        else:
            index = scan_dataset(request["dataset"], pool_general_events=action != "evidence", progress=progress)
        output = Path(request["output"])
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / "dataset-index.json", index)
        if action == "patterns":
            result = analyze_patterns(index, request["cache"], output, progress=progress)
        elif action == "train":
            from cowmata_tailring.algorithms.training import train_suite
            result = train_suite(index, request["cache"], output, progress=progress, codes=request.get("codes"), modality=request.get("modality", "motion"))
        elif action == "evidence":
            result = build_evidence(index, request["cache"], request["suite"], output, progress=progress)
        else:
            raise ValueError("Unknown algorithm action")
    atomic_json(Path(request["result"]), result)


if __name__ == "__main__":
    main()
