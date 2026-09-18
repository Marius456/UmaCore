"""Run the Windows official-news proxy on the private Tailscale interface."""
from pathlib import Path
import sys


def main() -> None:
    from proxy import entry_point

    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    sys.argv = [
        "proxy",
        "--hostname", "100.111.216.3",
        "--port", "8888",
        "--num-workers", "1",
        "--num-acceptors", "1",
        "--log-file", str(log_dir / "official-events-proxy.log"),
    ]
    entry_point()


if __name__ == "__main__":
    main()
