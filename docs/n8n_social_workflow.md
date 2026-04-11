# N8N Social Media Workflow — AURA Integration

AURA generates images and captions, then POSTs a webhook payload to N8N on the RUD server.
N8N handles the actual platform API calls.

## Configuration

Set `RUD_N8N_URL` in your `.env` file:

```env
RUD_N8N_URL=http://192.168.1.219:5678
```

If not set, AURA defaults to `http://192.168.1.219:5678`.
When N8N is unreachable, AURA saves a draft to `~/.aura/social_drafts/`.

---

## Webhook Payload Format

AURA POSTs to: `{RUD_N8N_URL}/webhook/aura-social`

```json
{
  "platform": "instagram",
  "type": "carousel",
  "images": [
    {
      "b64": "<base64-encoded JPEG>",
      "prompt": "Slide 1 of 5: claude code...",
      "index": 0
    }
  ],
  "captions": [
    "✨ Claude Code revolutionizes development... #claudeai #coding"
  ],
  "metadata": {
    "topic": "claude code",
    "count": 5,
    "timestamp": "2026-04-11T10:00:00Z",
    "source": "aura-bot"
  }
}
```

**Field details:**
- `platform`: `"instagram"` | `"twitter"` | `"linkedin"`
- `type`: `"carousel"` | `"post"` | `"thread"`
- `images[].b64`: Base64-encoded JPEG, 1080×1080px (square, ready for Instagram)
- `captions`: One caption per image (carousel) or one total (single post)
- `metadata.timestamp`: ISO 8601 UTC

---

## N8N Workflow Setup

### 1. Create the Webhook Trigger

1. Open N8N → New Workflow
2. Add **Webhook** node:
   - HTTP Method: POST
   - Path: `aura-social`
   - Authentication: None (local network) or Header Auth
   - Response Mode: When Last Node Finishes

### 2. Add a Switch Node (route by platform)

Connect to a **Switch** node:
- Field: `{{ $json.platform }}`
- Case 1: `instagram`
- Case 2: `twitter`
- Case 3: `linkedin`

### 3. Instagram Carousel Flow

Instagram requires 3 API calls per carousel:

**Node A — HTTP Request (upload each photo):**
```
POST https://graph.facebook.com/v19.0/{ig-user-id}/media
Body:
{
  "image_url": "...",  // OR upload from base64 via separate step
  "is_carousel_item": true,
  "access_token": "{{ $credentials.accessToken }}"
}
```
Use a **Loop Over Items** node to upload each image in `$json.images`.

**Node B — HTTP Request (create carousel container):**
```
POST https://graph.facebook.com/v19.0/{ig-user-id}/media
Body:
{
  "media_type": "CAROUSEL",
  "children": ["<id1>", "<id2>", ...],
  "caption": "{{ $json.captions[0] }}",
  "access_token": "{{ $credentials.accessToken }}"
}
```

**Node C — HTTP Request (publish):**
```
POST https://graph.facebook.com/v19.0/{ig-user-id}/media_publish
Body:
{
  "creation_id": "{{ $node['Node B'].json.id }}",
  "access_token": "{{ $credentials.accessToken }}"
}
```

Store `accessToken` in N8N Credentials (not in code).

### 4. Twitter/X Flow

Use **HTTP Request** node with Twitter API v2:
```
POST https://api.twitter.com/2/tweets
Headers: Authorization: Bearer {token}
Body: { "text": "{{ $json.captions[0] }}" }
```

For threads: loop over captions, reply to previous tweet ID.

### 5. LinkedIn Flow

Use **HTTP Request** node with LinkedIn API:
```
POST https://api.linkedin.com/v2/ugcPosts
Headers: Authorization: Bearer {token}
Body: LinkedIn UGC Post schema
```

### 6. Response Node

Add a **Respond to Webhook** node at the end:
```json
{
  "success": true,
  "post_url": "https://www.instagram.com/p/..."
}
```
AURA displays this URL to the user.

---

## Instagram Graph API Requirements

1. **Facebook Business Account** linked to Instagram Professional Account
2. **Facebook App** with `instagram_basic`, `instagram_content_publish`, `pages_show_list` permissions
3. **Long-lived User Access Token** (valid 60 days, refresh before expiry)
4. **Instagram User ID** (numeric, found via Graph API Explorer)

Get your IG User ID:
```
GET https://graph.facebook.com/v19.0/me/accounts?access_token=YOUR_TOKEN
```

All credentials go into N8N's **Credential Manager** — never in AURA code.

---

## Image Upload Options

Since AURA sends base64, N8N needs to decode and upload. Two approaches:

**Option A — N8N Code Node (decode b64 → binary):**
```javascript
const images = $json.images;
return images.map(img => ({
  json: { prompt: img.prompt, index: img.index },
  binary: {
    data: {
      data: img.b64,
      mimeType: 'image/jpeg',
      fileName: `slide_${img.index}.jpg`
    }
  }
}));
```

**Option B — Upload to temporary storage first (S3, Cloudflare R2, etc.):**
AURA can be configured to upload to a bucket and send URLs instead of b64.
Set `SOCIAL_IMAGE_BACKEND=s3` in `.env` (future feature).

---

## Example N8N Workflow JSON

Import this skeleton into N8N (Workflow → Import):

```json
{
  "name": "AURA Social Post",
  "nodes": [
    {
      "name": "Webhook",
      "type": "n8n-nodes-base.webhook",
      "parameters": {
        "path": "aura-social",
        "httpMethod": "POST",
        "responseMode": "lastNode"
      }
    },
    {
      "name": "Platform Switch",
      "type": "n8n-nodes-base.switch",
      "parameters": {
        "dataPropertyName": "platform",
        "rules": {
          "rules": [
            {"value": "instagram"},
            {"value": "twitter"},
            {"value": "linkedin"}
          ]
        }
      }
    },
    {
      "name": "Respond OK",
      "type": "n8n-nodes-base.respondToWebhook",
      "parameters": {
        "respondWith": "json",
        "responseBody": "={\"success\": true}"
      }
    }
  ],
  "connections": {
    "Webhook": {"main": [[{"node": "Platform Switch"}]]},
    "Platform Switch": {"main": [
      [{"node": "Instagram Flow"}],
      [{"node": "Twitter Flow"}],
      [{"node": "LinkedIn Flow"}]
    ]},
    "Instagram Flow": {"main": [[{"node": "Respond OK"}]]}
  }
}
```

Expand each platform node with the full API call chain described above.

---

## Testing Without N8N

If `RUD_N8N_URL` is not set or N8N is down, AURA:
1. Saves the post draft to `~/.aura/social_drafts/<platform>_<timestamp>.json`
2. Reports the draft path in Telegram so you can retry later

Draft format (no heavy b64 — just prompts and captions):
```json
{
  "platform": "instagram",
  "type": "carousel",
  "captions": ["caption 1", "caption 2"],
  "metadata": {"topic": "claude code", "count": 5},
  "image_prompts": ["Slide 1 of 5: ...", "Slide 2 of 5: ..."]
}
```
