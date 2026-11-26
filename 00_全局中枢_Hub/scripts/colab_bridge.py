"""Host-only adapter for google-colab-cli 0.8.0. Never starts interactive OAuth."""
import json
import os
import sys
from importlib.metadata import version


def main():
    if version('google-colab-cli') != '0.8.0':
        raise RuntimeError('CLI_VERSION: 请先核对升级后的适配器')
    from colab_cli import auth
    from colab_cli.common import state
    def needs_login(*args):
        raise RuntimeError('AUTH_REQUIRED')
    auth._run_remote_flow = needs_login
    operation, *args = sys.argv[1:]
    if operation == 'inspect':
        assignments = state.client.list_assignments()
        print(json.dumps([a.endpoint for a in assignments]))
    elif operation == 'endpoint':
        session = state.store.get(args[0]); print(session.endpoint if session else '')
    elif operation == 'release':
        name, endpoint = args
        if endpoint in {a.endpoint for a in state.client.list_assignments()}:
            state.client.unassign(endpoint)
        if endpoint in {a.endpoint for a in state.client.list_assignments()}:
            raise RuntimeError('RELEASE_PENDING')
        saved = state.store.get(name)
        if saved and saved.endpoint == endpoint: state.store.remove(name)
    else:
        from colab_cli.cli import main as cli_main
        sys.argv = ['colab',operation,*args]
        cli_main()

if __name__ == '__main__':
    try: main()
    except Exception as err:
        # Never echo tokens, remote exception bodies or an OAuth URL into the queue.
        import logging
        reason = str(err)
        label = 'AUTH_REQUIRED' if 'AUTH_REQUIRED' in reason else type(err).__name__
        print(label,file=sys.stderr)
        sys.exit(77 if label == 'AUTH_REQUIRED' else 1)
