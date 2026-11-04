import subprocess
import sys


def test_cpu_import_never_loads_torch_triton_or_cloud_sdks():
    code = ("import sys, bankql.analytics, bankql.analytics.mercer.core.pipeline, bankql.analytics.mercer.kernels.lsh_hash, "
            "bankql.analytics.mercer.kernels.schema_encode, bankql.analytics.mercer.kernels.result_score; "
            "bad = [m for m in ('torch', 'triton', 'anthropic', 'openai', 'google.generativeai') if m in sys.modules]; "
            "print('LOADED', bad); assert not bad")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PATH": "/usr/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
