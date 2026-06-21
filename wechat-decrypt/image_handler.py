#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WeChat image message handler for bot monitor.

Decrypts chat images received as local_type=3 messages:
1. Extract file MD5 from packed_info in message_resource.db
2. Locate corresponding .dat file in WeChat attach directory
3. Decrypt using v2_decrypt_file from decode_image
4. Save decoded image for VLM processing
"""

from __future__ import annotations

import glob
import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Optional

try:
    from decode_image import v2_decrypt_file
except ImportError:
    v2_decrypt_file = None


def extract_md5_from_packed_info(blob: Optional[bytes]) -> Optional[str]:
    """Extract file MD5 (ASCII hex string) from packed_info protobuf blob.

    packed_info layout for image messages:
      [4B: 12 22 0a 20] [32B: ASCII hex encoded MD5]
    Total is exactly 36 bytes.

    Returns:
        32-char hex string like 'bb2d0a0356e072db534774bd7e4da217', or None.
    """
    if not blob or not isinstance(blob, bytes):
        return None
    if len(blob) < 36:
        return None

    # Find the protobuf marker
    marker = b'\x12\x22\x0a\x20'
    idx = blob.find(marker)
    if idx < 0:
        return None

    start = idx + len(marker)
    if start + 32 > len(blob):
        return None

    md5_ascii = blob[start:start + 32].decode('ascii', errors='replace')
    # Validate it's hex
    if len(md5_ascii) == 32 and all(c in '0123456789abcdef' for c in md5_ascii.lower()):
        return md5_ascii.lower()
    return None


def extract_md5_from_packed_info_data(blob: Optional[bytes]) -> Optional[str]:
    """Extract file MD5 from Msg_xxx.packed_info_data (42-byte protobuf).

    Observed layout: 08 1f 10 02 1a 22 22 20 [32B ASCII hex MD5]
    Total 42 bytes.  The MD5 hex string starts at byte offset 8.

    Returns:
        32-char hex string, or None.
    """
    if not blob or not isinstance(blob, bytes):
        return None
    if len(blob) < 40:
        return None
    # Byte offset 8 is where the MD5 hex string begins
    try:
        md5_ascii = blob[8:40].decode('ascii', errors='replace')
    except Exception:
        return None
    if len(md5_ascii) == 32 and all(c in '0123456789abcdef' for c in md5_ascii.lower()):
        return md5_ascii.lower()
    return None


def find_dat_for_message(
    local_id: int,
    username: str,
    attach_base_dir: str,
    mrdb_path: Optional[str] = None,
    packed_info_data_bytes: Optional[bytes] = None,
) -> Optional[str]:
    """Full lookup chain: local_id → packed_info MD5 → .dat file path.

    Uses ChatName2Id to resolve username to chat_id, then queries
    MessageResourceInfo on (chat_id, message_local_id, message_local_type=3).
    If packed_info_data_bytes is provided (from Msg_xxx), skip message_resource.db.

    Args:
        local_id: message local_id from message_*.db
        username: sender/chatroom username (e.g. '47965620946@chatroom')
        attach_base_dir: WeChat msg/attach/ base directory
        mrdb_path: path to decrypted message_resource.db (optional if packed_info_data_bytes given)
        packed_info_data_bytes: raw packed_info_data from Msg_xxx table (42-byte protobuf)

    Returns:
        Full path to the .dat file, or None if not found.
    """
    file_md5 = None
    if packed_info_data_bytes:
        file_md5 = extract_md5_from_packed_info_data(packed_info_data_bytes)

    if not file_md5 and mrdb_path and os.path.exists(mrdb_path):
        con = sqlite3.connect(f'file:{mrdb_path}?mode=ro', uri=True)
        try:
            row = con.execute(
                'SELECT rowid FROM ChatName2Id WHERE user_name=?', (username,)
            ).fetchone()
            if row:
                chat_id = row[0]
                row = con.execute(
                    'SELECT packed_info FROM MessageResourceInfo WHERE chat_id=? AND message_local_id=? AND message_local_type=3',
                    (chat_id, local_id),
                ).fetchone()
                if row and row[0]:
                    file_md5 = extract_md5_from_packed_info(row[0])
        finally:
            con.close()

    if not file_md5:
        return None

    # Step 3: Locate .dat file in attach directory
    # Path: attach/<md5(username)>/<YYYY-MM>/Img/<file_md5>.dat
    username_md5 = hashlib.md5(username.encode()).hexdigest()
    attach_dir = os.path.join(attach_base_dir, username_md5)

    if not os.path.isdir(attach_dir):
        return None

    # Search recursively (covers all date subdirectories)
    # Prefer main file, then thumbnail
    for suffix in ['', '_t', '_h']:
        pattern = os.path.join(attach_dir, '**', 'Img', f'{file_md5}{suffix}.dat')
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]

    return None


def process_image_message(
    m: dict,
    t: dict,
    cfg: dict,
    mrdb_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    packed_info_data_bytes: Optional[bytes] = None,
) -> Optional[str]:
    """Process an image message: find .dat, decrypt, save to output dir.

    Args:
        m: message dict from row_to_dict, must have 'local_id' and 'local_type'==3
        t: target dict from config, must have 'username' (or 'name')
        cfg: full config dict, must have 'image_aes_key' and 'image_xor_key'
        mrdb_path: path to message_resource.db (auto-detected if None)
        output_dir: where to save decoded images (default: config['decoded_images_dir'])
        packed_info_data_bytes: raw packed_info_data from Msg_xxx table (42-byte protobuf)

    Returns:
        Path to decoded image file, or None if processing failed.
    """
    import logging
    _log = logging.getLogger('image_handler')

    if v2_decrypt_file is None:
        _log.warning('process_image_msg: v2_decrypt_file not imported')
        return None

    local_id = int(m.get('local_id') or 0)
    if local_id <= 0:
        _log.warning('process_image_msg: invalid local_id=%s', m.get('local_id'))
        return None

    username = t.get('username') or t.get('name') or ''
    if not username:
        _log.warning('process_image_msg: missing username/target')
        return None

    # Resolve paths
    wechat_data_dir = cfg.get('wechat_data_dir', '')
    if not wechat_data_dir:
        _log.warning('process_image_msg: wechat_data_dir missing in cfg')
        return None

    if mrdb_path is None:
        # Try decrypted copies: project dir first, then config.decrypted_dir, then wechat_data_dir parents
        project_root = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(project_root, 'decrypted', 'message', 'message_resource.db'),
        ]
        # Add config.decrypted_dir if available
        config_decrypted = cfg.get('decrypted_dir')
        if config_decrypted:
            candidates.append(os.path.join(config_decrypted, 'message', 'message_resource.db'))
        # Legacy fallbacks
        candidates.extend([
            os.path.join(wechat_data_dir, 'decrypted', 'message', 'message_resource.db'),
            os.path.join(os.path.dirname(wechat_data_dir), 'decrypted', 'message', 'message_resource.db'),
        ])
        mrdb_path = ''
        for c in candidates:
            if os.path.exists(c):
                mrdb_path = c
                break

    attach_base_dir = os.path.join(wechat_data_dir, 'msg', 'attach')

    _log.info('process_image_msg start local_id=%s username=%s data_dir=%s mrdb=%s packed=%s',
              local_id, username, wechat_data_dir, mrdb_path, packed_info_data_bytes is not None)

    # Step 1: Find .dat file (prefer packed_info_data_bytes, fall back to message_resource.db)
    dat_path = find_dat_for_message(
        local_id, username, attach_base_dir, mrdb_path,
        packed_info_data_bytes=packed_info_data_bytes,
    )
    if not dat_path:
        _log.warning('process_image_msg: .dat not found local_id=%s attach=%s mrdb=%s',
                     local_id, attach_base_dir, mrdb_path)
        return None
    _log.info('process_image_msg: .dat found path=%s', dat_path)

    # Step 2: Decrypt
    aes_key_hex = cfg.get('image_aes_key', '')
    xor_key = cfg.get('image_xor_key', 0)

    if not aes_key_hex:
        _log.warning('process_image_msg: image_aes_key missing')
        return None

    aes_key = aes_key_hex  # v2_decrypt_file handles string→bytes internally (16 ASCII chars→16 bytes)

    if output_dir is None:
        output_dir = cfg.get('decoded_images_dir', '')
    if not output_dir:
        output_dir = os.path.join(wechat_data_dir, '..', 'decoded_images')

    os.makedirs(output_dir, exist_ok=True)

    # Generate output filename: <username_truncated>_<local_id>.<fmt>
    safe_name = username.replace('@', '_').replace(':', '_')[:50]
    base_out = os.path.join(output_dir, f'{safe_name}_{local_id}')

    _log.info('process_image_msg: decrypting dat=%s out=%s key_len=%s xor=%s',
              dat_path, base_out, len(aes_key), xor_key)
    result_path, fmt = v2_decrypt_file(
        dat_path, out_path=base_out, aes_key=aes_key, xor_key=xor_key
    )

    if not result_path:
        _log.warning('process_image_msg: v2_decrypt_file returned None')
        return None
    _log.info('process_image_msg: decrypted result=%s fmt=%s', result_path, fmt)

    # Convert wxgf/hevc to JPEG for VLM compatibility
    if fmt in ('hevc', 'bin') or (os.path.exists(result_path) and open(result_path, 'rb').read(4) == b'wxgf'):
        try:
            jpg_path = f"{result_path}.jpg"
            converted = _convert_wxgf_to_jpeg(result_path, jpg_path)
            if converted and os.path.exists(jpg_path):
                _log.info('process_image_msg: converted to jpeg=%s', jpg_path)
                return jpg_path
            else:
                _log.warning('process_image_msg: wxgf/hevc conversion failed')
        except Exception as e:
            _log.warning('process_image_msg: wxgf/hevc conversion exception: %r', e)

    return result_path


def _convert_wxgf_to_jpeg(hevc_path: str, jpeg_path: str) -> str | None:
    """Convert wxgf/HEVC file to JPEG for VLM processing.

    wxgf format: wxgf header + ICC profile + HEVC NAL units
    Scans for HEVC Annex B VPS start code and decodes first frame via PyAV.
    """
    try:
        import av

        with open(hevc_path, 'rb') as f:
            data = f.read()

        # Scan for HEVC Annex B VPS start code: 00 00 00 01 40 01
        vps_sig = b'\x00\x00\x00\x01\x40\x01'
        hevc_start = data.find(vps_sig)
        if hevc_start < 0:
            # fallback: SPS (00 00 00 01 42 01)
            hevc_start = data.find(b'\x00\x00\x00\x01\x42\x01')
        if hevc_start < 0:
            return None

        # Extract HEVC Annex B stream and decode with PyAV
        h265_path = hevc_path + '.h265'
        with open(h265_path, 'wb') as f:
            f.write(data[hevc_start:])

        try:
            container = av.open(h265_path, format='hevc')
            for frame in container.decode(video=0):
                img = frame.to_image()
                img.save(jpeg_path, "JPEG", quality=90)
                container.close()
                return jpeg_path
            container.close()
        finally:
            if os.path.exists(h265_path):
                os.unlink(h265_path)

    except ImportError:
        pass
    except Exception:
        pass
    return None


def test_lookup_chain(
    local_id: int,
    username: str,
    wechat_data_dir: str,
) -> dict:
    """Diagnostic: test the full lookup chain and return detailed results."""
    result = {
        'local_id': local_id,
        'username': username,
        'steps': [],
        'success': False,
        'error': None,
    }

    # Try decrypted copy first (project-relative via __file__), fallback to encrypted
    project_decrypted_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypted')
    mrdb_path = os.path.join(wechat_data_dir, 'db_storage', 'message', 'message_resource.db')
    alt_decrypted = os.path.join(project_decrypted_dir, 'message', 'message_resource.db')
    if os.path.exists(alt_decrypted):
        mrdb_path = alt_decrypted
    if not os.path.exists(mrdb_path):
        result['error'] = f'message_resource.db not found: {mrdb_path}'
        return result

    attach_base_dir = os.path.join(wechat_data_dir, 'msg', 'attach')

    con = sqlite3.connect(mrdb_path)
    try:
        row = con.execute(
            'SELECT rowid FROM ChatName2Id WHERE user_name=?', (username,)
        ).fetchone()
        if not row:
            result['error'] = f'ChatName2Id: no entry for {username}'
            return result
        chat_id = row[0]
        result['steps'].append(f'chat_id={chat_id}')

        row = con.execute(
            'SELECT packed_info FROM MessageResourceInfo WHERE chat_id=? AND message_local_id=? AND message_local_type=3',
            (chat_id, local_id),
        ).fetchone()
        if not row or not row[0]:
            result['error'] = f'MessageResourceInfo: no type=3 row for chat_id={chat_id}, local_id={local_id}'
            return result

        packed = row[0]
        result['steps'].append(f'packed_info len={len(packed)} hex={packed[:20].hex()}...')

        file_md5 = extract_md5_from_packed_info(packed)
        if not file_md5:
            result['error'] = f'extract_md5_from_packed_info failed for blob {packed.hex()}'
            return result
        result['file_md5'] = file_md5
        result['steps'].append(f'extracted MD5={file_md5}')
    finally:
        con.close()

    username_md5 = hashlib.md5(username.encode()).hexdigest()
    attach_dir = os.path.join(attach_base_dir, username_md5)
    result['attach_dir'] = attach_dir

    if not os.path.isdir(attach_dir):
        result['error'] = f'attach dir not found: {attach_dir}'
        return result

    for suffix in ['', '_t', '_h']:
        pattern = os.path.join(attach_dir, '**', 'Img', f'{file_md5}{suffix}.dat')
        matches = glob.glob(pattern, recursive=True)
        if matches:
            result['dat_path'] = matches[0]
            result['steps'].append(f'found .dat: {matches[0]}')
            result['success'] = True
            return result

    result['error'] = f'no .dat found for MD5={file_md5} under {attach_dir}'
    return result


# ─────────────────────────────────────────
# mmx cli VLM 识图集成
# ─────────────────────────────────────────

import subprocess
import json
import os
import shutil


def _find_mmx_cli() -> str | None:
    """Locate mmx CLI executable via env var, common paths, or PATH.

    Search order:
      1. MMX_CLI_PATH environment variable
      2. Common npm global install locations
      3. PATH lookup via shutil.which
    """
    # 1. Environment variable
    env_path = os.environ.get('MMX_CLI_PATH')
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Common npm global paths
    common_paths = [
        r'C:\npm\node_global\mmx.cmd',
        os.path.expandvars(r'%APPDATA%\npm\mmx.cmd'),
        os.path.expandvars(r'%LOCALAPPDATA%\npm\mmx.cmd'),
        os.path.expandvars(r'%PROGRAMFILES%\nodejs\mmx.cmd'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mmx.cmd'),
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p

    # 3. PATH lookup
    found = shutil.which('mmx')
    if found:
        return found

    return None


def _decode_cli_output(data: bytes) -> str:
    """Decode CLI stdout/stderr, handling Windows encoding issues.

    Tries UTF-8 first, then GBK/GB2312/CP936 for Windows Chinese locale,
    finally falls back to UTF-8 with replacement characters.
    """
    if not data:
        return ''
    # Try UTF-8 first
    try:
        text = data.decode('utf-8')
        # Heuristic: if result has lots of replacement-looking sequences,
        # it might be GBK bytes mis-decoded; try GBK instead
        if text.count('�') < 3 and '��' not in text:
            return text
    except UnicodeDecodeError:
        pass
    # Try GBK family (Windows Chinese code page 936)
    for enc in ('gbk', 'gb2312', 'cp936'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    # Last resort
    return data.decode('utf-8', errors='replace')


def mmx_recognize_image(image_path: str, prompt: str = "简要的描述一下图片内容") -> str:
    """使用 mmx cli 调用 MiniMax VLM 识图，返回文字描述。

    Args:
        image_path: 本地图片路径
        prompt: 识图提示词

    Returns:
        图片的文字描述，失败时返回错误信息
    """
    if not os.path.isfile(image_path):
        return f"[VLM Error] image not found: {image_path}"

    mmx_cli = _find_mmx_cli()
    if not mmx_cli:
        return (
            "[VLM Error] mmx CLI not found. "
            "Install: npm install -g @minimaxi/mmx\n"
            "Or set env: MMX_CLI_PATH=C:\\path\\to\\mmx.cmd"
        )

    cmd = [mmx_cli, 'vision', 'describe', '--image', image_path, '--prompt', prompt]
    try:
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            cmd, capture_output=True, timeout=120, shell=False,
            startupinfo=startupinfo, creationflags=creationflags,
        )
        stdout = _decode_cli_output(r.stdout) if r.stdout else ''
        stderr = _decode_cli_output(r.stderr) if r.stderr else ''
        if r.returncode == 0:
            # mmx vision describe returns JSON with {"content": "...", "base_resp": {...}}
            # Try to parse JSON first
            try:
                data = json.loads(stdout.strip())
                content = data.get('content', '')
                if content:
                    return content.strip()
            except (json.JSONDecodeError, ValueError):
                pass
            # Fallback: extract text from raw output (filter ASCII art/logo lines)
            lines = [l.strip() for l in stdout.split('\n') if l.strip()]
            result_lines = [l for l in lines if not any(c in l for c in '██╗╚═╝╔╝')]
            if not result_lines:
                result_lines = [l for l in lines if l]
            vlm_text = '\n'.join(result_lines)
            return vlm_text if vlm_text else stdout.strip()
        else:
            err = stderr.strip() or stdout.strip()[:200]
            return f"[VLM Error] mmx cli exit={r.returncode}: {err}"
    except subprocess.TimeoutExpired:
        return "[VLM Error] mmx cli timed out after 120s"
    except Exception as e:
        return f"[VLM Error] {e}"


def run_vision_hook(image_path: str, hook_cmd: list[str] | str, timeout: int = 120) -> str:
    """Run a user-configured vision hook command to recognize an image.

    The hook command receives the image path as the last argument.
    Expected output: text description to stdout, or JSON with a 'content' field.

    Args:
        image_path: Path to the image file
        hook_cmd: Command to run (list of strings, or string)
        timeout: Max seconds to wait for the hook

    Returns:
        Image description text, or error message starting with '[Vision Hook Error]'
    """
    if not os.path.isfile(image_path):
        return f"[Vision Hook Error] image not found: {image_path}"

    if isinstance(hook_cmd, str):
        cmd = [hook_cmd, image_path]
    else:
        cmd = list(hook_cmd) + [image_path]

    try:
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, shell=False,
            startupinfo=startupinfo, creationflags=creationflags,
        )
        stdout = _decode_cli_output(r.stdout) if r.stdout else ''
        stderr = _decode_cli_output(r.stderr) if r.stderr else ''
        if r.returncode == 0:
            # Try JSON parse first (content field)
            try:
                data = json.loads(stdout.strip())
                content = data.get('content', '')
                if content:
                    return content.strip()
            except (json.JSONDecodeError, ValueError):
                pass
            # Fallback: return stdout directly
            text = stdout.strip()
            return text if text else "[Vision Hook Error] empty output"
        else:
            err = stderr.strip() or stdout.strip()[:200]
            return f"[Vision Hook Error] exit={r.returncode}: {err}"
    except subprocess.TimeoutExpired:
        return f"[Vision Hook Error] timed out after {timeout}s"
    except Exception as e:
        return f"[Vision Hook Error] {e}"
