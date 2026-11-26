#!/usr/bin/env ruby
# Offline builder: credentials stay in memory and in the specified private file.
require 'psych'
require 'json'
require 'open3'
require 'tempfile'
require 'fileutils'

abort('Usage: ruby build-residential-config.rb ORIGINAL.yaml PRIVATE_OUTPUT.yaml') unless ARGV.length == 2
begin
  original = Psych.safe_load(File.read(ARGV[0]), permitted_classes: [], permitted_symbols: [], aliases: true)
  generator = File.expand_path('mihomo.residential-only.js', __dir__)
  node_source = <<~JS
    const { main } = require(process.argv[1]);
    let input = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { input += chunk; });
    process.stdin.on('end', () => {
      try { process.stdout.write(JSON.stringify(main(JSON.parse(input)))); }
      catch (error) { process.stderr.write(error.message + '\\n'); process.exitCode = 1; }
    });
  JS
  output, error, status = Open3.capture3('node', '-e', node_source, generator,
                                       stdin_data: JSON.generate(original))
  abort('Configuration rejected: ' + error.strip) unless status.success?
  config = JSON.parse(output)
  destination = File.expand_path(ARGV[1])
  directory = File.dirname(destination)
  FileUtils.mkdir_p(directory, mode: 0700)
  raise 'Output symlink not allowed' if File.symlink?(destination)
  Tempfile.create(['.residential-', '.yaml'], directory) do |file|
    file.chmod(0600)
    file.write(Psych.dump(config))
    file.flush
    file.fsync
    File.rename(file.path, destination)
  end
  puts 'Private configuration generated locally (mode 0600); not activated.'
rescue StandardError => error
  # Never echo parse messages or source lines that might contain credentials.
  warn "Build failed (#{error.class}); no credential contents printed."
  exit 1
end
