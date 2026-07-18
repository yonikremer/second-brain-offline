"""
API: example-api
Purpose: one-sentence description of what this API provides.

Authentication notes:
- Base URL:
- Authentication method (Bearer, API key, OAuth2, etc.):
- Required environment variables or secrets (store in `.env`, never here):
- Token expiry / refresh behavior:
- Known network constraints (VPN, allow-listed IPs, etc.):

Example request snippet (adapt before use):
"""

import os

# Prefer loading secrets from a `.env` file in the project root.
# pip install python-dotenv  # if not already available
# from dotenv import load_dotenv
# load_dotenv()

# import requests
# headers = {
#     "Authorization": f"Bearer {os.environ.get('API_NAME_TOKEN')}",
#     "Content-Type": "application/json",
# }
# response = requests.get(
#     f"{os.environ.get('API_NAME_BASE_URL')}/v1/endpoint",
#     headers=headers,
# )
