#!/usr/bin/ruby
# Offline semantic checks and network-state helpers. Never print configuration data.
require 'psych'
require 'json'
require 'ipaddr'
require 'digest'
require 'open3'

module ClaudeShieldGuest
  HOME_NODE = '🛡️ AWS中转-美国家宽'.freeze
  RELAY = '198.51.100.24'.freeze
  PAYLOAD_FILES = %w[verge-mihomo mihomo-residential.yaml install-in-guest.sh
                     repair-guest-network.sh Install.command guest-network-common.sh
                     guest-config-check.rb].freeze
  LABEL = 'local.claudeshield.residential'.freeze
  class Rejected < StandardError; end

  def self.reject(message)
    raise Rejected, message
  end

  def self.regular_file(path)
    stat = File.lstat(path)
    reject('Expected a regular, non-symlink file.') unless stat.file? && !stat.symlink?
  end

  # Psych normally accepts duplicate keys; reject them before loading the document.
  def self.check_ast(node, depth = 0)
    reject('Configuration nesting is too deep.') if depth > 40
    reject('YAML aliases are not allowed.') if node.is_a?(Psych::Nodes::Alias)
    if node.is_a?(Psych::Nodes::Mapping)
      keys = []
      node.children.each_slice(2) do |key, _value|
        reject('Configuration mapping keys must be strings.') unless key.is_a?(Psych::Nodes::Scalar)
        reject('Duplicate configuration key.') if keys.include?(key.value)
        keys << key.value
      end
    end
    Array(node.respond_to?(:children) ? node.children : nil).each { |child| check_ast(child, depth + 1) }
  end

  def self.check_config(path)
    regular_file(path)
    reject('Configuration is too large.') if File.size(path) > 1_048_576
    text = File.read(path)
    tree = Psych.parse_stream(text)
    reject('Expected exactly one YAML document.') unless tree.children.length == 1
    check_ast(tree)
    actual = Psych.safe_load(text, permitted_classes: [], permitted_symbols: [], aliases: false)
    reject('Configuration root must be a mapping.') unless actual.is_a?(Hash)
    nodes = actual['proxies']
    reject('Expected exactly one residential node.') unless nodes.is_a?(Array) && nodes.length == 1 && nodes[0].is_a?(Hash)
    password = nodes[0]['password']
    reject('Residential credential is missing or malformed.') unless password.is_a?(String) && !password.empty? && password.bytesize <= 1024 && !password.include?("\0")
    dot = ["tls://1.1.1.1:853##{HOME_NODE}", "tls://1.0.0.1:853##{HOME_NODE}"]
    expected = {
      'mode' => 'rule', 'ipv6' => false, 'allow-lan' => false,
      'bind-address' => '127.0.0.1', 'interface-name' => 'en0',
      'log-level' => 'warning', 'keep-alive-idle' => 25, 'keep-alive-interval' => 10,
      'find-process-mode' => 'off', 'geo-auto-update' => false,
      'external-controller' => '', 'profile' => {'store-selected' => false, 'store-fake-ip' => false},
      'ntp' => {'enable' => false}, 'sniffer' => {'enable' => false},
      'tun' => {'enable' => true, 'stack' => 'gvisor', 'mtu' => 1380,
                'auto-route' => true, 'auto-detect-interface' => false, 'strict-route' => true,
                'route-exclude-address' => [RELAY + '/32'], 'dns-hijack' => ['any:53']},
      'listeners' => [{'name' => 'claude-dedicated-inbound', 'type' => 'mixed',
                       'listen' => '127.0.0.1', 'port' => 7894, 'udp' => false}],
      'proxies' => [{'name' => HOME_NODE, 'type' => 'ss', 'server' => RELAY, 'port' => 26333,
                     'cipher' => '2022-blake3-aes-128-gcm', 'password' => password, 'udp' => false}],
      'proxy-groups' => [],
      'dns' => {'enable' => true, 'listen' => '127.0.0.1:53', 'ipv6' => false,
                'enhanced-mode' => 'fake-ip', 'fake-ip-range' => '198.18.0.1/16',
                'fake-ip-filter' => ['*.lan', '*.local', '+.home.arpa', '+.localdomain'],
                'prefer-h3' => false, 'respect-rules' => false, 'use-hosts' => false,
                'use-system-hosts' => false, 'default-nameserver' => dot,
                'proxy-server-nameserver' => dot, 'nameserver' => dot},
      'rules' => ['NETWORK,udp,REJECT', 'DOMAIN-SUFFIX,push.apple.com,REJECT',
                  'DOMAIN-SUFFIX,ess.apple.com,REJECT', 'DOMAIN-SUFFIX,xp.apple.com,REJECT',
                  'MATCH,' + HOME_NODE]
    }
    reject('Configuration differs from the dedicated, single-path policy.') unless actual == expected
    true
  end

  def self.verify_payload(directory)
    manifest_path = File.join(directory, 'guest-payload-manifest.json')
    regular_file(manifest_path)
    reject('Payload manifest is too large.') if File.size(manifest_path) > 16_384
    manifest = JSON.parse(File.read(manifest_path))
    reject('Invalid payload manifest schema.') unless manifest.is_a?(Hash) && manifest.keys.sort == %w[files schema_version] && manifest['schema_version'] == 1
    files = manifest['files']
    reject('Payload file list does not match the approved guest package.') unless files.is_a?(Hash) && files.keys.sort == PAYLOAD_FILES.sort
    PAYLOAD_FILES.each do |name|
      expected = files[name]
      reject('Invalid payload digest.') unless expected.is_a?(String) && expected.match?(/\A[0-9a-f]{64}\z/)
      path = File.join(directory, name)
      regular_file(path)
      reject('Payload digest mismatch.') unless Digest::SHA256.file(path).hexdigest == expected
    end
    true
  end

  def self.ipv4(value)
    reject('Unsupported IPv4 network settings.') unless value.is_a?(String) && value.match?(/\A(?:[0-9]{1,3}\.){3}[0-9]{1,3}\z/) && IPAddr.new(value).ipv4?
    value
  rescue IPAddr::InvalidAddressError
    reject('Unsupported IPv4 network settings.')
  end

  def self.parse_network(service, info, dns_text, mtu_text, enabled_text)
    reject('Invalid network service name.') unless service.is_a?(String) && !service.empty? && service.bytesize < 256 && !service.match?(/[\r\n\0]/)
    lines = info.lines.map(&:strip)
    mode = case lines.first
           when 'DHCP Configuration' then 'dhcp'
           when 'Manual Configuration' then 'manual'
           else reject('Unsupported address mode; network settings were not changed.')
           end
    state = {'schema_version' => 1, 'service' => service, 'mode' => mode}
    if mode == 'manual'
      {'ip' => 'IP address:', 'mask' => 'Subnet mask:', 'router' => 'Router:'}.each do |key, prefix|
        values = lines.select { |line| line.start_with?(prefix) }
        reject('Ambiguous manual network settings.') unless values.length == 1
        state[key] = ipv4(values[0].delete_prefix(prefix).strip)
      end
      mask = IPAddr.new(state['mask']).to_i
      reject('Invalid manual subnet mask.') unless ((~mask & 0xffffffff) & ((~mask & 0xffffffff) + 1)).zero?
    else
      client = lines.select { |line| line.start_with?('Client ID:', 'DHCP Client ID:') }
      reject('Ambiguous DHCP client identifier.') if client.length > 1
      state['dhcp_client_id'] = client.empty? ? '' : client.first.sub(/\A(?:DHCP )?Client ID:/, '').strip
      reject('Unsupported DHCP client identifier.') if state['dhcp_client_id'].bytesize > 255 || state['dhcp_client_id'].match?(/[\r\n\0]/)
    end
    dns_lines = dns_text.lines.map(&:strip).reject(&:empty?)
    if dns_lines.length == 1 && dns_lines[0].start_with?("There aren't any DNS Servers set on ")
      state['dns'] = []
    else
      reject('Cannot recover DNS settings from this output.') if dns_lines.empty?
      state['dns'] = dns_lines.map do |address|
        reject('Invalid saved DNS address.') unless IPAddr.new(address).to_s && !address.match?(/[\s\/\0]/)
        address
      end
    end
    mtu = mtu_text.match(/Current Setting:\s*(\d+)/)
    reject('Cannot recover the original MTU.') unless mtu && (576..65_535).cover?(mtu[1].to_i)
    state['mtu'] = mtu[1].to_i
    state['enabled'] = case enabled_text.strip
                       when 'Enabled' then true
                       when 'Disabled' then false
                       else reject('Cannot recover network-service enabled state.')
                       end
    state
  rescue IPAddr::InvalidAddressError
    reject('Invalid saved network address.')
  end

  def self.restore_plan(state)
    reject('Invalid network-state receipt.') unless state.is_a?(Hash) && state['schema_version'] == 1
    # Revalidate receipts as carefully as the original command output.
    info = state['mode'] == 'manual' ? "Manual Configuration\nIP address: #{state['ip']}\nSubnet mask: #{state['mask']}\nRouter: #{state['router']}\n" : "DHCP Configuration\nClient ID: #{state['dhcp_client_id']}\n"
    reject('Unknown saved address mode.') unless %w[manual dhcp].include?(state['mode'])
    reject('Invalid DNS receipt.') unless state['dns'].is_a?(Array) && state['dns'].all? { |x| x.is_a?(String) }
    reject('Invalid service state receipt.') unless [true, false].include?(state['enabled'])
    dns_text = state['dns'].empty? ? "There aren't any DNS Servers set on this service.\n" : state['dns'].join("\n")
    checked = parse_network(state['service'], info, dns_text, "Current Setting: #{state['mtu']}", state['enabled'] ? 'Enabled' : 'Disabled')
    service = checked['service']
    first = if checked['mode'] == 'manual'
              ['-setmanual', service, checked['ip'], checked['mask'], checked['router']]
            else
              ['-setdhcp', service] + (checked['dhcp_client_id'].empty? ? [] : [checked['dhcp_client_id']])
            end
    [first, ['-setdnsservers', service] + (checked['dns'].empty? ? ['Empty'] : checked['dns']),
     ['-setMTU', service, checked['mtu'].to_s], ['-setnetworkserviceenabled', service, checked['enabled'] ? 'on' : 'off']].map { |args| ['/usr/sbin/networksetup'] + args }
  end

  def self.restore_network(state, runner = nil)
    if runner.nil?
      require_guest
      runner = lambda { |command| Open3.capture3(*command)[2].success? }
    end
    results = restore_plan(state).map { |command| runner.call(command) }
    reject('One or more network restore commands failed; retain the backup for recovery.') unless results.all?
    true
  end

  def self.disabled_state(text)
    entries = text.lines.each_with_object([]) do |line, result|
      found = line.match(/\A\s*"local\.claudeshield\.residential"\s*=>\s*(true|false)\s*\z/)
      result << (found[1] == 'true') if found
    end
    reject('Ambiguous launch service disabled state.') if entries.length > 1
    entries.empty? ? false : entries[0]
  end

  def self.snapshot(service, directory, loaded)
    reject('Invalid loaded-state observation.') unless %w[true false].include?(loaded)
    state = parse_network(service, File.read(File.join(directory, 'netinfo.txt')),
                          File.read(File.join(directory, 'dns.txt')), File.read(File.join(directory, 'mtu.txt')),
                          File.read(File.join(directory, 'enabled.txt')))
    state['service_loaded'] = loaded == 'true'
    state['service_disabled'] = disabled_state(File.read(File.join(directory, 'disabled.txt')))
    output = File.join(directory, 'network-state.json')
    File.open(output, File::WRONLY | File::CREAT | File::EXCL, 0600) { |file| file.write(JSON.generate(state) + "\n"); file.flush; file.fsync }
    true
  end

  def self.current_state(directory, service)
    parse_network(service, File.read(File.join(directory, 'netinfo.txt')),
                  File.read(File.join(directory, 'dns.txt')), File.read(File.join(directory, 'mtu.txt')),
                  File.read(File.join(directory, 'enabled.txt')))
  end

  def self.verify_network(directory, expected)
    actual = current_state(directory, expected['service'])
    keys = %w[service mode dns mtu enabled]
    keys += expected['mode'] == 'manual' ? %w[ip mask router] : %w[dhcp_client_id]
    reject('Network read-back does not match the saved or requested settings.') unless keys.all? { |key| actual[key] == expected[key] }
    true
  end

  def self.main(args)
    command = args.shift
    case command
    when 'check'
      reject('Usage: check CONFIG') unless args.length == 1
      check_config(args[0])
    when 'verify-payload'
      reject('Usage: verify-payload DIRECTORY') unless args.length == 1
      verify_payload(args[0])
    when 'snapshot'
      reject('Usage: snapshot SERVICE DIRECTORY LOADED') unless args.length == 3
      snapshot(*args)
    when 'field'
      reject('Usage: field RECEIPT FIELD') unless args.length == 2 && %w[service_loaded service_disabled enabled].include?(args[1])
      state = JSON.parse(File.read(args[0]))
      value = state[args[1]]
      reject('Invalid saved service state.') unless [true, false].include?(value)
      puts value ? 'true' : 'false'
    when 'restore-network'
      reject('Usage: restore-network RECEIPT') unless args.length == 1
      restore_network(JSON.parse(File.read(args[0])))
    when 'verify-disabled'
      reject('Usage: verify-disabled RECEIPT OBSERVATION') unless args.length == 2
      expected = JSON.parse(File.read(args[0]))['service_disabled']
      reject('Invalid saved disabled state.') unless [true, false].include?(expected)
      reject('Launch service disabled state did not restore.') unless disabled_state(File.read(args[1])) == expected
    when 'verify-restored'
      reject('Usage: verify-restored RECEIPT DIRECTORY') unless args.length == 2
      verify_network(args[1], JSON.parse(File.read(args[0])))
    when 'verify-applied'
      reject('Usage: verify-applied SERVICE DIRECTORY') unless args.length == 2
      verify_network(args[1], {'service' => args[0], 'mode' => 'manual', 'ip' => '10.77.0.3',
                               'mask' => '255.255.255.0', 'router' => '10.77.0.1',
                               'dns' => ['127.0.0.1'], 'mtu' => 1380, 'enabled' => true})
    else
      reject('Unknown guest helper action.')
    end
  end

  def self.require_guest
    reject('Native recovery only runs as root in the dedicated macOS guest.') unless Process.uid.zero?
    model, _error, status = Open3.capture3('/usr/sbin/sysctl', '-n', 'hw.model')
    reject('Native recovery only runs in the dedicated macOS guest.') unless status.success? && model.strip.start_with?('VirtualMac')
    interface, _error, status = Open3.capture3('/sbin/ifconfig', 'en0')
    reject('Dedicated guest interface not found.') unless status.success? && interface.match?(/\bether 52:54:00:77:00:03\b/)
    true
  end
end

if __FILE__ == $PROGRAM_NAME
  begin
    ClaudeShieldGuest.main(ARGV)
  rescue ClaudeShieldGuest::Rejected => error
    warn error.message
    exit 1
  rescue StandardError
    # Parser errors can contain source lines or credentials. Never echo them.
    warn 'Guest preflight failed; no configuration values were printed.'
    exit 1
  end
end
