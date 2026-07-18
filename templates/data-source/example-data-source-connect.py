"""
Data source: example-data-source
Purpose: one-sentence description of what this source contains.

Connection notes:
- Host / endpoint:
- Authentication method:
- Required environment variables or secrets (store in `.env`, never here):
- Default database / schema:
- Known network constraints (VPN, allow-listed IPs, etc.):

Example connection snippet (adapt before use):
"""

import os

# Prefer loading secrets from a `.env` file in the project root.
# pip install python-dotenv  # if not already available
# from dotenv import load_dotenv
# load_dotenv()

# import some_client
# client = some_client.connect(
#     host=os.environ.get("SOURCE_NAME_HOST"),
#     api_key=os.environ.get("SOURCE_NAME_API_KEY"),
#     database=os.environ.get("SOURCE_NAME_DATABASE"),
# )
