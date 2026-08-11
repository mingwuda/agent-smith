"""微信 iLink 图片加密数据的 AES-128-ECB 解密工具。

从 wechat_bot.py 拆分（2026-08-03 大文件治理）：纯函数、无类依赖，
供图片下载链路（_download_image_as_data_url）使用。
"""
import logging

logger = logging.getLogger(__name__)

# ── 纯 Python AES-128-ECB 解密 ──────────────────────────────────
# AES S-box (前向)
_AES_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]

# AES 逆 S-box (用于 InvSubBytes)
_AES_INV_SBOX = [0] * 256
for _i, _v in enumerate(_AES_SBOX):
    _AES_INV_SBOX[_v] = _i


def _aes_decrypt_block(block: bytes, expanded_key: list[list[int]]) -> bytes:
    """解密一个 16 字节 AES 块（ECB 模式下逐块调用）。"""
    state = list(block)
    nr = 10

    def inv_sub_bytes(s):
        return [_AES_INV_SBOX[b] for b in s]

    def inv_shift_rows(s):
        return [
            s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11],
        ]

    def inv_mix_columns(s):
        def galois_mul(a, b):
            p = 0
            for _ in range(8):
                if b & 1:
                    p ^= a
                hi = a & 0x80
                a = (a << 1) & 0xFF
                if hi:
                    a ^= 0x1B
                b >>= 1
            return p
        result = [0] * 16
        for i in range(4):
            c = s[i*4:(i+1)*4]
            # InvMixColumns matrix on GF(2^8):
            # [14 11 13  9]   [c0]
            # [ 9 14 11 13] * [c1]
            # [13  9 14 11]   [c2]
            # [11 13  9 14]   [c3]
            result[i*4]   = galois_mul(14, c[0]) ^ galois_mul(11, c[1]) ^ galois_mul(13, c[2]) ^ galois_mul(9, c[3])
            result[i*4+1] = galois_mul(9, c[0])  ^ galois_mul(14, c[1]) ^ galois_mul(11, c[2]) ^ galois_mul(13, c[3])
            result[i*4+2] = galois_mul(13, c[0]) ^ galois_mul(9, c[1])  ^ galois_mul(14, c[2]) ^ galois_mul(11, c[3])
            result[i*4+3] = galois_mul(11, c[0]) ^ galois_mul(13, c[1]) ^ galois_mul(9, c[2])  ^ galois_mul(14, c[3])
        return result

    def add_round_key(s, rk):
        return [s[i] ^ rk[i] for i in range(16)]

    # 初始轮密钥加（使用最后一轮密钥）
    state = add_round_key(state, expanded_key[nr])

    # 解密主循环
    for r in range(nr - 1, 0, -1):
        state = inv_shift_rows(state)
        state = inv_sub_bytes(state)
        state = add_round_key(state, expanded_key[r])
        state = inv_mix_columns(state)

    # 最后一轮（无 InvMixColumns）
    state = inv_shift_rows(state)
    state = inv_sub_bytes(state)
    state = add_round_key(state, expanded_key[0])

    return bytes(state)


def _aes_key_expansion(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展: 16 字节 → 11 轮密钥 (每轮 16 字节)"""
    rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
    # 初始 4 个字 (每个字 4 字节)
    w = [list(key[0:4]), list(key[4:8]), list(key[8:12]), list(key[12:16])]
    for i in range(4, 44):
        temp = w[i-1][:]
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]  # RotWord
            temp = [_AES_SBOX[b] for b in temp]  # SubWord
            temp[0] ^= rcon[i//4 - 1]
        w.append([w[i-4][j] ^ temp[j] for j in range(4)])
    # 将 44 个字展平为 11 个轮密钥，每个轮密钥 16 字节
    round_keys = []
    for r in range(11):
        rk = []
        for word in w[r*4:(r+1)*4]:
            rk.extend(word)
        round_keys.append(rk)
    return round_keys


def aes_decrypt_ecb(ciphertext: bytes, key_hex: str) -> bytes:
    """AES-128-ECB 解密

    使用系统 OpenSSL 命令解密。
    如果 OpenSSL 不可用或失败，返回原始数据（不做解密）。

    Args:
        ciphertext: 密文（长度应为 16 的倍数）
        key_hex: 32 字符十六进制密钥

    Returns:
        解密后的明文，或解密失败时的原始数据
    """
    import subprocess
    try:
        result = subprocess.run(
            ['openssl', 'enc', '-d', '-aes-128-ecb', '-K', key_hex.lower(), '-nosalt'],
            input=ciphertext,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass

    # 解密失败：返回原始数据（调用方会得到无效图片，API 会报错）
    logger.warning(
        "[AES] OpenSSL 解密失败，返回原始数据 (%d bytes)", len(ciphertext),
    )
    return ciphertext
