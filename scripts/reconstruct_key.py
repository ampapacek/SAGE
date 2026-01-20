import os

hex_string = "fd615e12071de1458ff62bfd41121006f7491f31e2f54d5da3b52ae15e02f05b"
secret_mask = bytes([0x55] * 32)
prefix = "sk-or-v1-"

target_file = ".env.example"
target_line_start = "CUSTOM_LLM_PROVIDER_1_API_KEY="

def xor_logic():
    raw_bytes = bytes.fromhex(hex_string)
    result_bytes = bytes([b ^ m for b, m in zip(raw_bytes, secret_mask)])
    final_key = prefix + result_bytes.hex()
    
    if not os.path.exists(target_file):
        with open(target_file, "w") as f:
            f.write(f"{target_line_start}{final_key}\n")
        return

    with open(target_file, "r") as f:
        lines = f.readlines()

    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(target_line_start):
            current_value = line.replace(target_line_start, "").strip()
            if not current_value:
                new_lines.append(f"{target_line_start}{final_key}\n")
                updated = True
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    if updated:
        with open(target_file, "w") as f:
            f.writelines(new_lines)

if __name__ == "__main__":
    xor_logic()