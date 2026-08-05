"""PB-OPS-1 offline private-beta invitation command line."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.private_beta_invitation_operations import (  # noqa: E402
    PrivateBetaInvitationOperationError,
    create_private_beta_invitation,
    revoke_private_beta_invitation,
    status_private_beta_invitation,
)


_COMMON_OPTIONS = (
    "--config",
    "--database",
    "--invitation-key-file",
)
_CREATE_OPTIONS = _COMMON_OPTIONS + (
    "--request-id",
    "--expires-at",
    "--credential-output",
)
_REFERENCE_OPTIONS = _COMMON_OPTIONS + ("--invitation-id",)
_SUCCESS_FRAME = "PB_OPS_1_SUCCESS_V1"
_SUCCESS_RECORD_MAX_BYTES = 4_096


class _SyntaxFailure(Exception):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class _Command:
    name: str
    json_output: bool
    values: dict[str, str]


def _parse(arguments) -> _Command:
    if type(arguments) is not list or any(type(item) is not str for item in arguments):
        raise _SyntaxFailure()
    tokens = list(arguments)
    json_output = bool(tokens and tokens[0] == "--json")
    if json_output:
        tokens.pop(0)
    if not tokens or tokens[0] not in {"create", "status", "revoke"}:
        raise _SyntaxFailure()
    name = tokens.pop(0)
    expected = _CREATE_OPTIONS if name == "create" else _REFERENCE_OPTIONS
    if len(tokens) != len(expected) * 2:
        raise _SyntaxFailure()
    values = {}
    for index in range(0, len(tokens), 2):
        option = tokens[index]
        value = tokens[index + 1]
        if option not in expected or option in values or not value:
            raise _SyntaxFailure()
        values[option] = value
    if set(values) != set(expected):
        raise _SyntaxFailure()
    return _Command(name=name, json_output=json_output, values=values)


def _read_hidden_email_pair() -> tuple[str, str]:
    if os.name == "nt":
        return _read_windows_console_pair()
    if os.name == "posix":
        return _read_posix_tty_pair()
    raise OSError()


def _read_windows_console_pair() -> tuple[str, str]:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetConsoleMode.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    kernel32.ReadConsoleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadConsoleW.restype = wintypes.BOOL
    kernel32.WriteConsoleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteConsoleW.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    generic_read = 0x80000000
    generic_write = 0x40000000
    share_read = 0x1
    share_write = 0x2
    open_existing = 3
    echo_input = 0x4
    line_input = 0x2
    processed_input = 0x1
    input_handle = kernel32.CreateFileW(
        "CONIN$",
        generic_read | generic_write,
        share_read | share_write,
        None,
        open_existing,
        0,
        None,
    )
    if input_handle == invalid:
        raise OSError()
    output_handle = kernel32.CreateFileW(
        "CONOUT$",
        generic_write,
        share_read | share_write,
        None,
        open_existing,
        0,
        None,
    )
    if output_handle == invalid:
        kernel32.CloseHandle(input_handle)
        raise OSError()
    original = wintypes.DWORD()
    restored = False
    values = []
    try:
        if not kernel32.GetConsoleMode(input_handle, ctypes.byref(original)):
            raise OSError()
        hidden_mode = (original.value | line_input | processed_input) & ~echo_input
        if not kernel32.SetConsoleMode(input_handle, hidden_mode):
            raise OSError()
        confirmed = wintypes.DWORD()
        if (
            not kernel32.GetConsoleMode(input_handle, ctypes.byref(confirmed))
            or confirmed.value & echo_input
        ):
            raise OSError()
        for prompt in ("Invitation email: ", "Confirm invitation email: "):
            _windows_console_write(kernel32, output_handle, prompt)
            values.append(_windows_console_line(kernel32, input_handle))
            _windows_console_write(kernel32, output_handle, "\r\n")
        if not kernel32.SetConsoleMode(input_handle, original.value):
            raise OSError()
        restored = True
        return values[0], values[1]
    finally:
        if not restored:
            kernel32.SetConsoleMode(input_handle, original.value)
        values[:] = []
        output_closed = kernel32.CloseHandle(output_handle)
        input_closed = kernel32.CloseHandle(input_handle)
        if not output_closed or not input_closed:
            if sys.exc_info()[0] is None:
                raise OSError()


def _windows_console_write(kernel32, handle, text: str):
    from ctypes import wintypes

    written = wintypes.DWORD()
    if not kernel32.WriteConsoleW(
        handle,
        text,
        len(text),
        ctypes.byref(written),
        None,
    ) or written.value != len(text):
        raise OSError()


def _windows_console_line(kernel32, handle) -> str:
    from ctypes import wintypes

    capacity = 322
    buffer = ctypes.create_unicode_buffer(capacity)
    count = wintypes.DWORD()
    if not kernel32.ReadConsoleW(
        handle,
        buffer,
        capacity,
        ctypes.byref(count),
        None,
    ):
        raise OSError()
    text = buffer[: count.value]
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    else:
        _drain_windows_console_line(kernel32, handle)
        raise OSError()
    if len(text) > 320:
        raise OSError()
    return text


def _drain_windows_console_line(kernel32, handle):
    from ctypes import wintypes

    while True:
        buffer = ctypes.create_unicode_buffer(64)
        count = wintypes.DWORD()
        if not kernel32.ReadConsoleW(
            handle,
            buffer,
            len(buffer),
            ctypes.byref(count),
            None,
        ):
            return
        if "\n" in buffer[: count.value]:
            return


def _read_posix_tty_pair() -> tuple[str, str]:
    import termios

    flags = os.O_RDWR | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/dev/tty", flags)
    original = None
    restored = False
    values = []
    try:
        os.set_inheritable(descriptor, False)
        if os.get_inheritable(descriptor) or not os.isatty(descriptor):
            raise OSError()
        original = termios.tcgetattr(descriptor)
        hidden = list(original)
        hidden[3] &= ~(termios.ECHO | getattr(termios, "ECHONL", 0))
        hidden[3] |= termios.ICANON
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, hidden)
        confirmed = termios.tcgetattr(descriptor)
        if confirmed[3] & termios.ECHO:
            raise OSError()
        for prompt in (b"Invitation email: ", b"Confirm invitation email: "):
            _posix_write_all(descriptor, prompt)
            values.append(_posix_tty_line(descriptor))
            _posix_write_all(descriptor, b"\n")
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, original)
        restored = True
        return values[0], values[1]
    finally:
        cleanup_failed = False
        if original is not None and not restored:
            try:
                termios.tcsetattr(descriptor, termios.TCSAFLUSH, original)
            except BaseException:
                cleanup_failed = True
        values[:] = []
        try:
            os.close(descriptor)
        except BaseException:
            cleanup_failed = True
        if cleanup_failed:
            raise OSError("console_cleanup_failed") from None


def _posix_write_all(descriptor: int, payload: bytes):
    offset = 0
    while offset < len(payload):
        count = os.write(descriptor, payload[offset:])
        if count <= 0:
            raise OSError()
        offset += count


def _posix_tty_line(descriptor: int) -> str:
    payload = bytearray()
    while len(payload) <= 322:
        chunk = os.read(descriptor, 1)
        if not chunk:
            raise OSError()
        if chunk == b"\n":
            break
        payload.extend(chunk)
    else:
        while os.read(descriptor, 1) not in {b"", b"\n"}:
            pass
        raise OSError()
    if payload.endswith(b"\r"):
        payload.pop()
    if len(payload) > 320:
        raise OSError()
    try:
        return payload.decode("utf-8", "strict")
    finally:
        payload[:] = b"\x00" * len(payload)


def _execute(command: _Command):
    values = command.values
    common = {
        "configuration_path": values["--config"],
        "database_path": values["--database"],
        "invitation_key_path": values["--invitation-key-file"],
    }
    if command.name == "create":
        return create_private_beta_invitation(
            **common,
            request_id=values["--request-id"],
            expires_at=values["--expires-at"],
            credential_output=values["--credential-output"],
            hidden_email_reader=_read_hidden_email_pair,
        )
    if command.name == "status":
        return status_private_beta_invitation(
            **common,
            invitation_reference=values["--invitation-id"],
        )
    return revoke_private_beta_invitation(
        **common,
        invitation_reference=values["--invitation-id"],
    )


def _success_text(fields: dict[str, str]) -> str:
    order = (
        "operation",
        "outcome",
        "invitation_reference",
        "email_hint",
        "created_at",
        "expires_at",
        "status",
    )
    return " ".join(f"{name}={fields[name]}" for name in order if name in fields)


def _success_record(fields: dict[str, str], *, json_output: bool) -> str:
    payload_json = json.dumps(
        fields,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_bytes = payload_json.encode("ascii")
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if json_output:
        record = json.dumps(
            {
                "frame": "pb_ops_1_success_v1",
                "payload": fields,
                "payload_bytes": len(payload_bytes),
                "payload_sha256": payload_sha256,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    else:
        payload = _success_text(fields)
        if "\r" in payload or "\n" in payload:
            raise ValueError("invalid_success_payload")
        record = (
            f"{_SUCCESS_FRAME} bytes={len(payload.encode('utf-8'))} "
            f"sha256={hashlib.sha256(payload.encode('utf-8')).hexdigest()} "
            f"payload={payload}\n"
        )
    if not 1 <= len(record.encode("utf-8")) <= _SUCCESS_RECORD_MAX_BYTES:
        raise ValueError("invalid_success_record_size")
    return record


def _publish_success_record(result, *, json_output: bool):
    fields = result.approved_fields()
    record = _success_record(fields, json_output=json_output)
    result._begin_delivery()
    written = sys.stdout.write(record)
    if type(written) is not int or written != len(record):
        raise OSError("incomplete_success_record")
    sys.stdout.flush()
    result._confirm_delivery()


def _render_error(error: PrivateBetaInvitationOperationError, *, json_output: bool):
    fields = {"error": error.code}
    if error.code == "COMMITTED_RETRY_REQUIRED":
        fields.update(
            {
                "durable_mutation": "MAY_ALREADY_HAVE_OCCURRED",
                "exact_retry_only": True,
                "recovery": "REPEAT_EXACT_INVOCATION",
                "success_requires": "COMPLETE_FRAME_AND_EXIT_0",
            }
        )
        if error.cleanup_incomplete:
            fields["cleanup"] = "INCOMPLETE"
    if error.status is not None:
        fields["status"] = error.status
    if json_output:
        line = json.dumps(fields, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    else:
        line = error.code
        if error.code == "COMMITTED_RETRY_REQUIRED":
            line += (
                " durable_mutation=MAY_ALREADY_HAVE_OCCURRED"
                " exact_retry_only=true"
                " recovery=REPEAT_EXACT_INVOCATION"
                " success_requires=COMPLETE_FRAME_AND_EXIT_0"
            )
            if error.cleanup_incomplete:
                line += " cleanup=INCOMPLETE"
        if error.status is not None:
            line += " status=" + error.status
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def main(arguments=None) -> int:
    if arguments is None:
        arguments = sys.argv[1:]
    json_output = bool(arguments and arguments[0] == "--json")
    try:
        command = _parse(arguments)
        json_output = command.json_output
        result = _execute(command)
        try:
            _publish_success_record(result, json_output=json_output)
        except BaseException:
            if result._durable_delivery:
                raise PrivateBetaInvitationOperationError(
                    "COMMITTED_RETRY_REQUIRED",
                    8,
                ) from None
            raise PrivateBetaInvitationOperationError(
                "INTERNAL_FAILURE",
                7,
            ) from None
        return 0
    except _SyntaxFailure:
        error = PrivateBetaInvitationOperationError("INVALID_INPUT", 2)
    except PrivateBetaInvitationOperationError as exc:
        error = exc
    except BaseException:
        error = PrivateBetaInvitationOperationError("INTERNAL_FAILURE", 7)
    _render_error(error, json_output=json_output)
    return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
