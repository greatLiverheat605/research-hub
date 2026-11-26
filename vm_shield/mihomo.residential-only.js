// Dedicated-core configuration generator. This REPLACES the supplied configuration.
// Use in an isolated environment; ordinary DMIT/direct routing is intentionally absent.
// This is NOT an operating-system firewall. See 严格家宽模式.md before deployment.
function main(config) {
  const HOME = "🛡️ AWS中转-美国家宽";
  const SERVER = "198.51.100.24";
  const PORT = 26333;
  const matches = (config?.proxies || []).filter(p => p.name === HOME);
  if (matches.length !== 1) throw new Error("完整原配置必须包含唯一的家宽节点。");
  const original = matches[0];
  if (original.type !== "ss" || original.server !== SERVER || original.port !== PORT ||
      original.cipher !== "2022-blake3-aes-128-gcm" ||
      typeof original.password !== "string" || original.password.length === 0) {
    throw new Error("家宽节点与已指定的 SS-2022 中转端点不符，或缺少凭据。");
  }
  if (original.plugin || original["dialer-proxy"]) {
    throw new Error("家宽节点存在额外插件或上游代理，需先核实实际链路。");
  }
  // Construct from an allowlist; discard inherited providers, DNS fallbacks,
  // hidden listeners, hosts rewrites, health checks, and alternative routes.
  const home = {
    name: HOME, type: "ss", server: SERVER, port: PORT,
    cipher: original.cipher, password: original.password,
    udp: false
  };
  // The proxy endpoint AND resolver addresses are IP literals: no local DNS bootstrap.
  // Every resolver connection is explicitly assigned to the sole residential node.
  const dot = ["tls://1.1.1.1:853#" + HOME, "tls://1.0.0.1:853#" + HOME];
  return {
    mode: "rule",
    ipv6: false,
    "allow-lan": false,
    "bind-address": "127.0.0.1",
    // The dedicated VM has exactly one physical interface, with a fixed MAC.
    "interface-name": "en0",
    "log-level": "warning",
    "keep-alive-idle": 25,
    "keep-alive-interval": 10,
    "find-process-mode": "off",
    "geo-auto-update": false,
    "external-controller": "",
    profile: { "store-selected": false, "store-fake-ip": false },
    ntp: { enable: false },
    sniffer: { enable: false },
    // Guest-only capture. The separate gateway enforces egress when TUN/core stops.
    // Do not activate this configuration on the user's ordinary host Mac.
    tun: {
      enable: true, stack: "gvisor", mtu: 1380, "auto-route": true,
      "auto-detect-interface": false, "strict-route": true,
      "route-exclude-address": [SERVER + "/32"],
      "dns-hijack": ["any:53"]
    },
    // Omit listener-level 'proxy' so explicit proxy traffic on 127.0.0.1:7894
    // traverses the exact same ruleset (UDP reject, Apple reject, sole AWS MATCH) as TUN.
    listeners: [{
      name: "claude-dedicated-inbound", type: "mixed", listen: "127.0.0.1",
      port: 7894, udp: false
    }],
    proxies: [home],
    "proxy-groups": [],
    dns: {
      // macOS scoped DNS may bind to en0 and bypass TUN routes. Point the
      // guest network service directly at this standard-port loopback resolver.
      enable: true, listen: "127.0.0.1:53", ipv6: false,
      "enhanced-mode": "fake-ip", "fake-ip-range": "198.18.0.1/16",
      "fake-ip-filter": ["*.lan", "*.local", "+.home.arpa", "+.localdomain"],
      "prefer-h3": false, "respect-rules": false,
      "use-hosts": false, "use-system-hosts": false,
      "default-nameserver": [...dot],
      "proxy-server-nameserver": [...dot],
      nameserver: [...dot]
    },
    rules: [
      "NETWORK,udp,REJECT",
      "DOMAIN-SUFFIX,push.apple.com,REJECT",
      "DOMAIN-SUFFIX,ess.apple.com,REJECT",
      "DOMAIN-SUFFIX,xp.apple.com,REJECT",
      "MATCH," + HOME
    ]
  };
}

if (typeof module !== "undefined") module.exports = { main };
