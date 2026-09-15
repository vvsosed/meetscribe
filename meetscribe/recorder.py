"""Construct and drive a pw-record subprocess.

pw-record resamples to our target format and writes raw PCM to stdout, which
is why nothing in this package does sample-rate conversion.
"""

from __future__ import annotations

from .types import TARGET_RATE

PW_RECORD = "pw-record"


def format_properties(props: dict[str, str]) -> str:
    """Render the --properties argument.

    PipeWire parses this as JSON-ish. Every value must stay quoted: an
    unquoted space inside a value splits the property and it is dropped
    without any error message.
    """
    body = " ".join(f'{key}="{value}"' for key, value in props.items())
    return "{ " + body + " }"


def build_argv(
    *,
    node_name: str,
    media_name: str,
    target: int | None = None,
    capture_sink: bool = False,
    autoconnect: bool = True,
    latency: str = "100ms",
) -> list[str]:
    props = {"node.name": node_name, "media.name": media_name}
    if capture_sink:
        props["stream.capture.sink"] = "true"
    if not autoconnect:
        props["node.autoconnect"] = "false"

    argv = [
        PW_RECORD,
        "--rate",
        str(TARGET_RATE),
        "--channels",
        "1",
        "--format",
        "s16",
        "--latency",
        latency,
        "--properties",
        format_properties(props),
        "--raw",
    ]
    if target is not None:
        argv += ["--target", str(target)]
    argv.append("-")
    return argv
