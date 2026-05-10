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
    if v2_decrypt_file is None:
        return None

    local_id = int(m.get('local_id') or 0)
    if local_id <= 0:
        return None

    username = t.get('username') or t.get('name') or ''
    if not username:
        return None

    # Resolve paths
    wechat_data_dir = cfg.get('wechat_data_dir', '')
    if not wechat_data_dir:
        return None

    if mrdb_path is None:
        # Try decrypted copies: project dir first, then wechat_data_dir/decrypted
        project_root = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(project_root, 'decrypted', 'message', 'message_resource.db'),
            os.path.join(wechat_data_dir, 'decrypted', 'message', 'message_resource.db'),
            os.path.join(os.path.dirname(wechat_data_dir), 'decrypted', 'message', 'message_resource.db'),
        ]
        mrdb_path = ''
        for c in candidates:
            if os.path.exists(c):
                mrdb_path = c
                break

    attach_base_dir = os.path.join(wechat_data_dir, 'msg', 'attach')

    # Step 1: Find .dat file (prefer packed_info_data_bytes, fall back to message_resource.db)
    dat_path = find_dat_for_message(
        local_id, username, attach_base_dir, mrdb_path,
        packed_info_data_bytes=packed_info_data_bytes,
    )
    if not dat_path:
        return None

    # Step 2: Decrypt
    aes_key_hex = cfg.get('image_aes_key', '')
    xor_key = cfg.get('image_xor_key', 0)

    if not aes_key_hex:
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

    result_path, fmt = v2_decrypt_file(
        dat_path, out_path=base_out, aes_key=aes_key, xor_key=xor_key
    )

    if result_path:
        return result_path
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
