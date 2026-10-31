import os, time

def test_probe():
    print(f"PROBE pid={os.getpid()} mono={time.monotonic():.1f} uptime={open('/proc/uptime').read().split()[0]}")
