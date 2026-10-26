# Lookup API Documentation

## Base URL

`https://api.teamsecret.in`

The same Base URL is used for all API requests.

### Health Check

```http
GET https://api.teamsecret.in/
```

Expected response:

```json
{
  "status": "ok",
  "service": "lookup-api"
}
```

### Interactive Swagger Documentation

`https://api.teamsecret.in/docs`

---

## Time Zone

All dates and times displayed by the Telegram bot, including order history, API plan expiry, pass expiry, approvals, and related notices, use **India Standard Time (IST / Asia/Kolkata, UTC+05:30)**.

---

## Authentication

Every lookup request requires your API key in the `X-API-Key` HTTP header.

```http
X-API-Key: YOUR_API_KEY
```

The API key is unique to your account and must be kept private. The full key is shown when your API plan is activated or when you regenerate the key from **My API**.

The server stores a secure hash of the API key rather than the original full key, so the same full key cannot be displayed again later.

---

# 1. Mobile Number Lookup

Use this endpoint to search an exact Indian mobile number.

## Endpoint

```http
GET /api/lookup?number=9876543210
```

Full URL example:

```text
https://api.teamsecret.in/api/lookup?number=9876543210
```

## Query Parameter

- `number` — required.
- Must contain a valid 10-digit Indian mobile number.

## cURL Example

```bash
curl -G 'https://api.teamsecret.in/api/lookup' \
  --data-urlencode 'number=9876543210' \
  -H 'X-API-Key: YOUR_API_KEY'
```

## Python Example

```python
import requests

url = "https://api.teamsecret.in/api/lookup"
headers = {
    "X-API-Key": "YOUR_API_KEY"
}
params = {
    "number": "9876543210"
}

response = requests.get(
    url,
    headers=headers,
    params=params,
    timeout=30,
)

print(response.status_code)
print(response.json())
```

## JavaScript / Node.js Example

```javascript
const url = new URL("https://api.teamsecret.in/api/lookup");
url.searchParams.set("number", "9876543210");

const response = await fetch(url, {
  headers: {
    "X-API-Key": "YOUR_API_KEY"
  }
});

const data = await response.json();
console.log(response.status);
console.log(data);
```

---

# 2. ID Lookup

Use this endpoint to search an exact 12-digit record ID.

## Endpoint

```http
GET /api/id?id=123456789012
```

Full URL example:

```text
https://api.teamsecret.in/api/id?id=123456789012
```

## Query Parameter

- `id` — required.
- Must be exactly 12 digits.
- Partial IDs are not accepted.

## cURL Example

```bash
curl -G 'https://api.teamsecret.in/api/id' \
  --data-urlencode 'id=123456789012' \
  -H 'X-API-Key: YOUR_API_KEY'
```

## Python Example

```python
import requests

url = "https://api.teamsecret.in/api/id"
headers = {
    "X-API-Key": "YOUR_API_KEY"
}
params = {
    "id": "123456789012"
}

response = requests.get(
    url,
    headers=headers,
    params=params,
    timeout=30,
)

print(response.status_code)
print(response.json())
```

## JavaScript / Node.js Example

```javascript
const url = new URL("https://api.teamsecret.in/api/id");
url.searchParams.set("id", "123456789012");

const response = await fetch(url, {
  headers: {
    "X-API-Key": "YOUR_API_KEY"
  }
});

const data = await response.json();
console.log(response.status);
console.log(data);
```

---

# Success Response

Successful mobile and ID lookup requests use the same response structure.

HTTP `200` example:

```json
{
  "success": true,
  "type": "mobile",
  "query": "9876543210",
  "count": 1,
  "data": [
    {
      "name": "Example",
      "mobile": "9876543210"
    }
  ]
}
```

For ID lookup, `type` will be `id` and `query` will contain the submitted 12-digit ID.

The fields inside `data` depend on the record returned by the lookup service and may vary between records.

---

# Error Responses

All API errors use this format:

```json
{
  "success": false,
  "error": "Error message"
}
```

## `400 Bad Request`

Returned when the lookup input is invalid.

Examples:

```json
{
  "success": false,
  "error": "Invalid mobile number"
}
```

```json
{
  "success": false,
  "error": "ID must be exactly 12 digits"
}
```

## `401 Unauthorized`

The API key is missing, invalid, expired, or no longer active.

```json
{
  "success": false,
  "error": "Invalid or expired API key"
}
```

## `404 Not Found`

No matching record was found.

```json
{
  "success": false,
  "error": "No records found"
}
```

## `429 Too Many Requests`

The technical rate limit was exceeded.

```json
{
  "success": false,
  "error": "Rate limit exceeded"
}
```

Wait briefly before sending more requests.

## `502 Bad Gateway`

The upstream lookup service returned an authentication or processing failure.

## `503 Service Unavailable`

The lookup service is temporarily unavailable or timed out. Retry later.

---

# API Plan and Limits

The API plan is valid for **30 days** from activation or renewal.

The monthly request count is unlimited, but technical rate limits and fair-use protections still apply to keep the service stable.

This means:

- There is no fixed monthly request quota.
- A per-minute technical request limit may apply.
- Excessive parallel requests may be limited.
- Flooding, abusive automation, API-key sharing, and resale are not allowed.
- Access can be temporarily suspended or revoked for abuse or security risks.

When the API plan expires, requests using that API key stop working until the plan is renewed.

---

# API Key Management

Open **My API** in the Telegram bot or use:

```text
/myapi
```

You can view:

- API plan status.
- Expiry date/time in IST.
- API key prefix.
- Current API Base URL.

## Lost API Key

If you lose the full API key, use **Regenerate API Key** from My API.

When a key is regenerated:

1. The previous API key is revoked immediately.
2. A new API key is generated.
3. Your existing API plan expiry remains unchanged.
4. The new full key is sent in a separate Telegram message.

Save the new key securely because the same full key cannot be shown again later.

---

# Security Recommendations

Do not expose your API key in:

- Public GitHub repositories.
- Telegram groups or channels.
- Screenshots.
- Public configuration files.
- Frontend/browser JavaScript that other users can inspect.

For a bot or backend application, keep the API key in an environment variable.

Example:

```env
LOOKUP_API_KEY=YOUR_API_KEY
```

Then read that environment variable in your application instead of hard-coding the key directly into source code.

---

# Quick Reference

## Mobile Lookup

```text
GET https://api.teamsecret.in/api/lookup?number=9876543210
```

## ID Lookup

```text
GET https://api.teamsecret.in/api/id?id=123456789012
```

## Required Header

```text
X-API-Key: YOUR_API_KEY
```

## Swagger

```text
https://api.teamsecret.in/docs
```

---

# Telegram Bot Commands

- `/apidocs` — download the complete API documentation as a Markdown file.
- `/myapi` — view API plan status and manage your API key.
- `/orders` — view purchase/order history.
- `/plans` — view available purchase plans.
