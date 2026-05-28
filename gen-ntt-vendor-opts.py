#!/usr/bin/env python3
"""
Generate NTT DHCPv6 Vendor-specific (option 17, Enterprise 210) sub-options
for Kea DHCPv6 server config.

Interactive script. Just run:
    python3 gen-ntt-vendor-opts.py

Sub-options derived:
  201: HGW WAN MAC (6 bytes binary)
  202: Phone number (ASCII bytes)
  204: SIP registration domain in DNS wire format (e.g. ntt-west.ne.jp)
  210: Verinfo URL host in DNS wire format (e.g. www.verinfo.hgw.flets-west.jp)
"""

import json
import re
import sys


REGION_DEFAULTS = {
    "west": {
        "sip_domain": "ntt-west.ne.jp",
        "verinfo_host": "www.verinfo.hgw.flets-west.jp",
    },
    "east": {
        "sip_domain": "ntt-east.ne.jp",
        "verinfo_host": "www.verinfo.hgw.flets-east.jp",
    },
}


def mac_to_hex(mac: str) -> str:
    """34:38:aa:aa:aa:aa -> 3438aaaaaaaaaaaa"""
    cleaned = re.sub(r"[:\-\s]", "", mac).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", cleaned):
        raise ValueError(f"Invalid MAC: {mac}")
    return cleaned


def phone_to_hex(phone: str) -> str:
    cleaned = re.sub(r"[\s\-]", "", phone)
    if not re.fullmatch(r"[0-9]+", cleaned):
        raise ValueError(f"Invalid phone number (digits only): {phone}")
    return cleaned.encode("ascii").hex()


def domain_to_wire(domain: str) -> str:
    """
    DNS wire format: each label prefixed by its length byte, terminated by 0x00.
    ntt-west.ne.jp -> 086e74742d77657374026e65026a7000
    """
    domain = domain.strip(".")
    if not domain:
        raise ValueError("Empty domain")
    parts = []
    for label in domain.split("."):
        b = label.encode("ascii")
        if len(b) > 63:
            raise ValueError(f"Label too long: {label}")
        if not b:
            raise ValueError(f"Empty label in {domain}")
        parts.append(f"{len(b):02x}{b.hex()}")
    return "".join(parts) + "00"


def ask(prompt: str, default: str = "", validate=None) -> str:
    """Ask with optional default. Re-asks on validation failure."""
    while True:
        suffix = f" (default {default})" if default else ""
        ans = input(f"{prompt}{suffix}: ").strip()
        if not ans and default:
            ans = default
        if not ans:
            print("  Required.", file=sys.stderr)
            continue
        if validate:
            try:
                validate(ans)
            except ValueError as e:
                print(f"  Error: {e}", file=sys.stderr)
                continue
        return ans


def ask_choice(prompt: str, choices: list, default: str) -> str:
    """Ask a multiple-choice question."""
    while True:
        ans = input(f"{prompt} [{'/'.join(choices)}] (default {default}): ").strip().lower()
        if not ans:
            return default
        if ans in choices:
            return ans
        print(f"  Choose one of: {', '.join(choices)}", file=sys.stderr)


def build(mac: str, phone: str, sip_domain: str, verinfo_host: str) -> dict:
    return {
        "201": {"label": "HGW MAC",      "value": mac,          "hex": mac_to_hex(mac)},
        "202": {"label": "Phone number", "value": phone,        "hex": phone_to_hex(phone)},
        "204": {"label": "SIP domain",   "value": sip_domain,   "hex": domain_to_wire(sip_domain)},
        "210": {"label": "Verinfo host", "value": verinfo_host, "hex": domain_to_wire(verinfo_host)},
    }


def kea_snippet(parts: dict) -> str:
    snippet = {
        "option-def": [
            {"code": int(code), "name": f"ntt-{code}", "space": "vendor-210", "type": "binary"}
            for code in ("201", "202", "204", "210")
        ],
        "option-data": [
            {"name": "vendor-opts", "data": "210"},
        ] + [
            {
                "always-send": True,
                "name": f"ntt-{code}",
                "space": "vendor-210",
                "data": parts[code]["hex"],
            }
            for code in ("201", "202", "204", "210")
        ],
    }
    return json.dumps(snippet, indent=4, ensure_ascii=False)


def main():
    print("=== NTT HGW Vendor-specific (option 17) generator ===\n")

    mac = ask("HGW WAN MAC (e.g. 34:38:aa:aa:aa:aa)", validate=mac_to_hex)
    phone = ask("Phone number (digits only, e.g. 0878100000)", validate=phone_to_hex)
    region = ask_choice("Region", ["west", "east"], default="west")

    defaults = REGION_DEFAULTS[region]
    sip_domain = ask("SIP domain", default=defaults["sip_domain"], validate=domain_to_wire)
    verinfo_host = ask("Verinfo host", default=defaults["verinfo_host"], validate=domain_to_wire)

    parts = build(mac, phone, sip_domain, verinfo_host)

    print("\n=== Sub-options (hex values) ===")
    for code, info in parts.items():
        print(f"  {code} ({info['label']:14s} = {info['value']!r:40s}) -> {info['hex']}")

    print("\n=== Kea config snippet (paste under Dhcp6) ===")
    print(kea_snippet(parts))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
