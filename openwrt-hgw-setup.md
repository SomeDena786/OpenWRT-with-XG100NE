# OpenWRT 配下に NTT HGW (ひかり電話) を置く

OpenWRT で MAP-E を終端しつつ、配下に NTT ひかり電話 HGW (XG-100NE等) を置いて電話も動かす。
大半はClaudeに書かせてます。
odhcpdだとPDのオプションの選択肢が狭いので、HGWにはKeaで喋らせてます。

### 物理トポロジ
```
ONU ── eth2 (OpenWRT) ── eth3 ── HGW WAN
                │
                eth1 (OpenWRT LAN) ── 通常PC等(10.0.0.0/8; 00::/64)
```

### 環境
- NTT西日本(香川県)
- So-net光 10ギガ ファミリータイプ
- ひかり電話契約 + v6プラス(240b:240::/26)

- OpenWRT (NIC:X520-DA2/USB-NIC)
- XG-100NE(Ver.04.00.0008)
- 10G-EPON ONU (2025/12製造)


### 必要パッケージ
```sh
apk update
apk add tc-full kmod-sched kmod-sched-core \
        kea-dhcp6 kea-ctrl kea-uci kea-lfc \
        jq tcpdump python3
```


---

## 1. 直結キャプチャでHGW固有データ取得 

> **★ Vendor-specific (option 17) の中身は契約者固有**。
> 計算データが正しいか確認するために、 HGW を ONU 直結してキャプチャと突き合わせると安全。

### 取得すべき情報 (DHCPv6 Reply パケット内)
- **IA_PD prefix** (例: `240b:24xx:xxxx:xxxx::/56`)
- **DNS sub-options** (Option 23)
- **Domain Search List** (Option 24)
- **SIP Server IPv6** (Option 22)
- **SNTP servers** (Option 31)
- **Vendor-specific Option 17 (Enterprise 210)** — HGW固有
  - sub-option 201: HGW WAN MAC
  - sub-option 202: 電話番号
  - sub-option 204: SIP登録ドメイン (e.g. ntt-west.ne.jp)
  - sub-option 210: verinfo URL ホスト



---

## 2. OpenWRT 基本設定

### `/etc/config/network`

```
config interface 'WAN6' 
    option proto 'dhcpv6'
    option device 'eth2'                # ONU側NIC
    option reqaddress 'try'
    option reqprefix 'auto'             # /56を要求
    option norelease '1'
    option reqopts '17 21 22 23 24 31 56 64 67'

config interface 'v6pd'
    option proto 'static'
    option device 'eth3'                # HGW側NIC (L3アドレスは不要)



# /60 reverse route (HGW向け) - ★HGW起動後にWAN MAC側を指定
config route6
    option interface 'v6pd'
    option target '240b:24xx:xxxx:xx40::/60'    # Keaが配るプレフィックス
    option gateway 'fe80::aaaa:aaff:feaa:aaaa' # ★HGW WAN MAC側 (LAN MAC ではない)
```

### `/etc/config/firewall` 

```
config zone
    option name 'through'
    option input 'ACCEPT'
    option output 'ACCEPT'
    option forward 'REJECT'
    list network 'v6pd'

config forwarding
    option src 'through'
    option dest 'wan'

config forwarding
    option src 'wan'
    option dest 'through'
```

---

## 3. eth2↔eth3 L2透過 (IPv4 + ARP のみ)

### 必須: promisc + TC mirred

`/etc/rc.local` に追記:

```sh
#!/bin/sh
# ★ promiscuous モード必須 (これ無いとNICがHGW向けunicast frameを破棄)
ip link set eth2 promisc on
ip link set eth3 promisc on

# TC mirred: IPv4(0x0800) + ARP(0x0806) を双方向にリダイレクト
tc qdisc del dev eth2 ingress 2>/dev/null
tc qdisc del dev eth3 ingress 2>/dev/null
tc qdisc add dev eth2 ingress
tc qdisc add dev eth3 ingress
tc filter add dev eth2 ingress protocol ip  matchall action mirred egress redirect dev eth3
tc filter add dev eth3 ingress protocol ip  matchall action mirred egress redirect dev eth2
tc filter add dev eth2 ingress protocol arp matchall action mirred egress redirect dev eth3
tc filter add dev eth3 ingress protocol arp matchall action mirred egress redirect dev eth2
exit 0
```

```sh
chmod +x /etc/rc.local
sh /etc/rc.local
```

### 検証

```sh
ip link show eth2 | grep -o PROMISC   # → PROMISC が出ればOK
ip link show eth3 | grep -o PROMISC
tc filter show dev eth2 ingress       # 4ルール出ればOK
```

>  NIC は通常自分の MAC 宛フレームしか受け取らない。HGW WAN MAC 宛のユニキャストフレーム (DHCPv4 ACK, ARP Reply, SIP応答 等) は promisc 無効だと NIC レベルで破棄され、TC mirred が処理する前に消える。

---

## 4. Kea DHCPv6 サーバ (HGW向け配信)

### 4.1 ディレクトリ作成

```sh
mkdir -p /var/lib/kea && chmod 750 /var/lib/kea
mkdir -p /var/run/kea && chmod 750 /var/run/kea
```

### 4.2 Vendor-specific (option 17) サブオプション生成

直結キャプチャと突き合わせる代わりに、対話スクリプトで生成可能(なおちゃんと動くかどうかは未知数。東日本はたぶんダメ)

```sh
python3 tools/gen-ntt-vendor-opts.py
```

プロンプト:
- HGW WAN MAC (e.g. `34:38:aa:aa:aa:aa`)
- 電話番号 (digits only, e.g. `0878100000`)
- Region (west/east)
- SIP domain (デフォルト: `ntt-west.ne.jp`)
- Verinfo host (デフォルト: `www.verinfo.hgw.flets-west.jp`)

出力で `option-def` + `option-data` の JSON スニペットがそのまま得られるので Kea config に貼る。

### 4.3 `/etc/kea/kea-dhcp6.conf` テンプレート

```json
{
"Dhcp6": {
    "interfaces-config": { "interfaces": [ "eth3" ] },
    "control-socket": {
        "socket-type": "unix",
        "socket-name": "kea6-ctrl-socket"
    },
    "lease-database": {
        "type": "memfile",
        "persist": true,
        "name": "/var/lib/kea/kea-leases6.csv",
        "lfc-interval": 3600
    },
    "renew-timer": 7200,
    "rebind-timer": 10800,
    "preferred-lifetime": 12600,
    "valid-lifetime": 14400,

    "option-def": [
        { "code": 201, "name": "ntt-201", "space": "vendor-210", "type": "binary" },
        { "code": 202, "name": "ntt-202", "space": "vendor-210", "type": "binary" },
        { "code": 204, "name": "ntt-204", "space": "vendor-210", "type": "binary" },
        { "code": 210, "name": "ntt-210", "space": "vendor-210", "type": "binary" }
    ],

    "option-data": [
        { "name": "vendor-opts", "data": "210" },

        { "always-send": true, "name": "ntt-201", "space": "vendor-210",
          "data": "<gen-ntt-vendor-opts.py 出力をペースト>" },
        { "always-send": true, "name": "ntt-202", "space": "vendor-210",
          "data": "<同上>" },
        { "always-send": true, "name": "ntt-204", "space": "vendor-210",
          "data": "<同上>" },
        { "always-send": true, "name": "ntt-210", "space": "vendor-210",
          "data": "<同上>" },

        { "name": "dns-servers",
          "data": "2001:a7ff:5f01:1::a, 2001:a7ff:5f01::a" },   #この辺は西日本での話。東日本はアドレス違うと思う。
        { "name": "domain-search",
          "data": "flets-west.jp, flets-west.jp.iptvf.jp" },    #この辺は西日本での話。東日本はアドレス違うと思う。
        { "name": "sip-server-addr",
          "data": "2001:a7ff:310b:200::4" },                    #この辺は西日本での話。東日本はアドレス違うと思う。
        { "code": 31, "space": "dhcp6", "csv-format": true, 
          "data": "2001:a7ff:102::a, 2001:a7ff:102::b" }        #この辺は西日本での話。東日本はアドレス違うと思う。
    ],

    "subnet6": [{
        "id": 1,
        "interface": "eth3",
        "subnet": "fe80::/64",
        "pd-pools": [{
            "prefix": "240b:24xx:xxxx:xx40::",                   #HGWに配りたいプレフィックスを指定
            "prefix-len": 58,
            "delegated-len": 60
        }]
    }],

    "loggers": [{
        "name": "kea-dhcp6",
        "output-options": [{ "output": "syslog" }],
        "severity": "INFO"
    }]
}
}
```

### 4.4 サービス起動

```sh
uci set kea.dhcp6.disabled='0'
uci commit kea
kea-dhcp6 -t /etc/kea/kea-dhcp6.conf    # 構文チェック
/etc/init.d/kea enable
/etc/init.d/kea start
ss -lnup | grep 547                      # eth3でlisten確認
```

---

## 5. odhcpd で HGW 向け RA 配信

`/etc/config/dhcp` に v6pd セクション:

```
config dhcp 'v6pd'
    option interface 'v6pd'
    option ignore '0'
    option dhcpv6 'disabled'           # ← DHCPv6はKeaが処理
    option ndp 'disabled'
    option dhcpv4 'disabled'
    option ra 'server'
    option ra_default '1'              # ★ lifetime>0 で配信 (必須)
    option ra_flags 'managed-config other-config'
```

```sh
uci commit dhcp
/etc/init.d/odhcpd restart
```

### 検証 (eth3 で RA確認)

```sh
tcpdump -i eth3 -nn -vv 'icmp6 and ip6[40]=134' -c 1 -G 30 -W 1
# → router lifetime が 0 でない (例: 1800s) こと
```

---

## 6. PD 自動同期 hotplug　（たぶんいらない。基本的にプレフィックスが変わることはない）

NGN から降りる /56 が変わったとき Kea config を書き換える。

`/etc/hotplug.d/iface/90-kea-pd-sync`:

```sh
#!/bin/sh
[ "$INTERFACE" = "WAN6" ] || exit 0
[ "$ACTION" = "ifup" ] || [ "$ACTION" = "ifupdate" ] || exit 0

PD=$(ifstatus WAN6 | jsonfilter -e '@["ipv6-prefix"][0].address')
[ -z "$PD" ] && exit 0

# /56の中の/60起点 (sub-id 4を使う場合): "...4d00::" → "...4d40::"
BASE=$(echo "$PD" | sed 's/00::$//')
HGW_PFX="${BASE}40::"

CONF=/etc/kea/kea-dhcp6.conf
TMP=$(mktemp)
jq --arg pfx "$HGW_PFX" \
   '(.Dhcp6.subnet6[0]."pd-pools"[0].prefix) = $pfx' \
   "$CONF" > "$TMP" && mv "$TMP" "$CONF"
chmod 640 "$CONF"

/etc/init.d/kea reload 2>/dev/null || /etc/init.d/kea restart
logger -t kea-pd-sync "Updated to ${HGW_PFX}/60"
```

```sh
chmod +x /etc/hotplug.d/iface/90-kea-pd-sync
```

---

## 7. 動作確認 (順番に)

### 7.1 OpenWRT 基本疎通

```sh
# WAN6でPD取れてる?
ifstatus WAN6 | jsonfilter -e '@["ipv6-prefix"][0].address'

# MAP-EでIPv4出てる?
ping -c 3 1.1.1.1
ifstatus WAN4 | jsonfilter -e '@["ipv4-address"]'
```

### 7.2 NGN内サーバへの疎通

```python
python3 -c "
import socket
for h,p,proto in [
  ('2001:a7ff:5f01:1::a',53,'udp'),         # DNS
  ('2001:a7ff:102::a',123,'udp'),           # NTP
  ('2001:a7ff:ff61:1002::11',443,'tcp')]:   # ACS
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM if proto=='tcp' else socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        if proto=='tcp':
            s.connect((h,p)); print(f'{proto.upper()} {h}:{p} OK')
        else:
            s.sendto(b'\\x1b'+47*b'\\x00',(h,p)); s.recvfrom(2048)
            print(f'{proto.upper()} {h}:{p} OK')
    except Exception as e: print(f'{proto.upper()} {h}:{p} FAIL ({e})')
"
```

### 7.3 HGW 接続後の検証

```sh
# Keaログ (SARR成立しているか)
logread -e kea | tail -10
# DHCP6_PD_LEASE_ALLOC / REUSE で /60が見えればOK

# TC mirredカウンタが両方向で増えているか
tc -s filter show dev eth2 ingress | grep -E "rule hit|Sent" | head
tc -s filter show dev eth3 ingress | grep -E "rule hit|Sent" | head

# HGW WAN MACのNDP解決状態
ip -6 neigh show dev eth3
# fe80::xxxx router REACHABLE/STALE が出ればOK

# IPv6 forwarding 統計
cat /proc/net/snmp6 | grep -E "InNoRoutes|OutNoRoutes"
# InNoRoutes が増え続けないこと
```

### 7.4 HGW WebGUI確認

- 情報 → DHCPクライアント取得情報
  - 現在の時刻: 数分で正常な時刻 (UTC) に補正されるはず
  - IPv4アドレス: 取得済み
  - IPv6プレフィックス: /60取得済み
- 情報 → 通話ログ
  - 「レジスタ成功 200OK」が記録される
- 受話器を上げて「ツー」発信音 → 117 などにテスト発信

---

## 8. トラブル時の優先確認順

| 症状 | 確認 |
|------|------|
| TC mirred が 0個 | promisc 無効。`ip link set promisc on` |
| HGW が ARP 応答もらえない | eth2 ingress mirred カウンタが増えてるか |
| HGW 時刻 2019/01/01 | NTP IPv6 経路問題 (大体 promisc 原因の連鎖) |
| SIP REGISTER タイムアウト | IPv4戻り経路 = 同上 |
| RA配信されない | `dhcp.v6pd.ra_default='1'` が抜けてないか |
| HGW向け経路無効 | route6 gateway が**WAN MAC側**か (LAN MAC は NG) |


---

