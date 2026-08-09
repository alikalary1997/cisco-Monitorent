import os
from waitress import serve
from app import app

print(f"Current PATH: {os.environ.get('PATH')}")  # Debug
print(f"Telnet exists: {os.path.exists('/usr/bin/telnet')}")  # Debug

serve(
    app,
    host='0.0.0.0',
    port=5000,
    threads=4,
    ident="Flask Monitor" 
)
