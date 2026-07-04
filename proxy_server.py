import subprocess
import sys

# This starts a proxy on all interfaces on port 8888
if __name__ == '__main__':
    from proxy import entry_point
    # --hostname 0.0.0.0 makes it listen for your server's connection
    # --port 8888 is the port it will use
    sys.argv = ['proxy', '--hostname', '0.0.0.0', '--port', '8888']
    entry_point()