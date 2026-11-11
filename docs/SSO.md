# Signing in with your own sign-in (SSO)

A DeviceHub server (docs/SERVER.md) can use the sign-in you already have in front of your services instead of its
own password: Authelia, Authentik, oauth2-proxy or anything else a reverse proxy can ask with **forward auth**. The
proxy asks the sign-in about each request, and passes the ones it lets through on to DeviceHub with the person in
headers. DeviceHub then logs in the members of one group.

Anything that can reach DeviceHub's port without the proxy could send those headers too. So the proxy also adds a
**secret** that only it and DeviceHub know, and DeviceHub believes the headers only beside it.

## What the proxy has to do

On the hub's signed-in address (say `https://devicehub.example.net`), for every request:

1. **Delete** the client's own copies of the person's headers and of the secret header.
2. **Ask the sign-in.** Not signed in: send the browser to the sign-in page, as usual.
3. **Pass the person on** in headers: the username, the groups (comma or `|` separated), and the display name if
   there is one.
4. **Add the secret** in its header, then send the request to DeviceHub's port.

Step 1 matters wherever the sign-in might not answer with a header (a path you let through without signing in, a
sign-in that leaves a header out): without it, a client's own copy would arrive as if the sign-in had sent it.

## DeviceHub's settings

| Variable | Default | Meaning |
|---|---|---|
| `DEVHUB_SSO_SECRET` | | The secret, at least 16 characters. Not set: the sign-in is off and the page uses `DEVHUB_PASSWORD`. |
| `DEVHUB_SSO_SECRET_FILE` | | Instead of `DEVHUB_SSO_SECRET`: a file holding it (a Docker secret, say) |
| `DEVHUB_SSO_SECRET_HEADER` | `X-DevHub-SSO` | The header the proxy puts the secret in |
| `DEVHUB_SSO_USER_HEADER` | `Remote-User` | The username |
| `DEVHUB_SSO_GROUPS_HEADER` | `Remote-Groups` | The groups, comma or `\|` separated |
| `DEVHUB_SSO_NAME_HEADER` | `Remote-Name` | The display name, on the **Log out** button (the username without it) |
| `DEVHUB_SSO_GROUP` | `admins` | The group whose members are logged in |
| `DEVHUB_SSO_IGNORE_USERS` | | Users that are never people (scripts' keys): space-separated patterns, e.g. `svc-*` |
| `DEVHUB_SSO_URL` | | The signed-in address. Set, the port only serves devices and agents (below). |
| `DEVHUB_SSO_LOGOUT` | | Where **Log out** goes: the sign-in's logout page |

Make a secret with `openssl rand -hex 32` (or `python3 -c "import secrets; print(secrets.token_hex(32))"`).

With Docker, put them in `sso.env` next to `compose.yaml`, which the compose file loads (`DEVHUB_SSO_ENV` in `.env`
can name another file, one a tool writes for you). Don't also list them under `environment:`: there an empty
`${VAR:-}` would win over the file. The container reads the file when it's made, so after a change run
`docker compose up -d` (a restart keeps the old values).

```bash
# sso.env (keep it out of git)
DEVHUB_SSO_SECRET=6f0c...the secret...
DEVHUB_SSO_URL=https://devicehub.example.net
DEVHUB_SSO_LOGOUT=https://auth.example.net/logout
```

And in `.env`, `DEVHUB_PUBLIC_URL` must be the port's LAN address (`http://192.168.1.10:8765`): it's what the beacon
port you download calls, and a handheld can't sign in.

## What DeviceHub does with them

- **Through the sign-in** (the secret header matches, compared in constant time): a member of `DEVHUB_SSO_GROUP`
  whose name doesn't match `DEVHUB_SSO_IGNORE_USERS` is logged in, with no DeviceHub login. The page shows who, and
  **Log out** goes to `DEVHUB_SSO_LOGOUT`. Anyone else signed in gets the password login, if `DEVHUB_PASSWORD` is set.
- **Without the secret**, the person's headers are ignored, whatever they say. The `Host` header and the client's
  address never decide anything.
- **The port itself** (`http://<server>:8765`), with `DEVHUB_SSO_URL` set, serves only what devices and agents use:
  the beacon's announces, files the hub serves to devices (`/dl/`), `/devhub.py`, `/agent-guide.md`, and API calls with
  a token (`X-DevHub-Token`). A browser page there gets a redirect to the same path on `DEVHUB_SSO_URL`, and the
  password login there doesn't count. Without `DEVHUB_SSO_URL` the port keeps its password login beside the sign-in.
- A request another site's page makes (`Sec-Fetch-Site: same-site` or `cross-site`) never acts as the user, except to
  read: other names under your domain are the same site, so the sign-in's cookie comes along with them.

If the sign-in is down, unset `DEVHUB_SSO_URL` (and set `DEVHUB_PASSWORD`) to log in on the port again.

**When it doesn't work**, the hub's log (`docker logs devicehub`) says why, once each: at startup
`sign-in: on (...)` or `sign-in: off (...)`, then for example "X-DevHub-SSO doesn't match DEVHUB_SSO_SECRET" or "the
sign-in passed no Remote-Groups for alice". The login page only says that the sign-in isn't set up right.

## Recipes

Each one puts the hub at `https://devicehub.example.net`, with DeviceHub on `192.168.1.10:8765`. Put your own secret
where it says `<the secret>`, the same one as `DEVHUB_SSO_SECRET`.

### Traefik and Authelia

Authelia answers forward auth at `/api/authz/forward-auth` (4.38 and newer) with `Remote-User`, `Remote-Groups`,
`Remote-Name` and `Remote-Email`. In Traefik's dynamic configuration (a header set to `""` is deleted):

```yaml
http:
  middlewares:
    devicehub-clean:
      headers:
        customRequestHeaders:
          X-DevHub-SSO: ""
          Remote-User: ""
          Remote-Groups: ""
          Remote-Name: ""
          Remote-Email: ""
    authelia:
      forwardAuth:
        address: "http://authelia:9091/api/authz/forward-auth"
        trustForwardHeader: true
        authResponseHeaders: ["Remote-User", "Remote-Groups", "Remote-Name", "Remote-Email"]
    devicehub-secret:
      headers:
        customRequestHeaders:
          X-DevHub-SSO: "<the secret>"
  routers:
    devicehub:
      rule: "Host(`devicehub.example.net`)"
      entryPoints: ["websecure"]
      tls: {}
      middlewares: ["devicehub-clean", "authelia", "devicehub-secret"]
      service: devicehub
  services:
    devicehub:
      loadBalancer:
        servers:
          - url: "http://192.168.1.10:8765"
```

`DEVHUB_SSO_LOGOUT=https://auth.example.net/logout`. Authelia's own rules (`access_control`) decide who may reach the
name at all; `DEVHUB_SSO_GROUP` decides who of them is logged in to DeviceHub.

### Caddy and Authelia

```
devicehub.example.net {
	request_header -X-DevHub-SSO
	request_header -Remote-User
	request_header -Remote-Groups
	request_header -Remote-Name
	request_header -Remote-Email

	forward_auth authelia:9091 {
		uri /api/authz/forward-auth
		copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
	}

	reverse_proxy 192.168.1.10:8765 {
		header_up X-DevHub-SSO "<the secret>"
	}
}
```

### nginx and oauth2-proxy

oauth2-proxy (with `--set-xauthrequest`) answers `/oauth2/auth` with `X-Auth-Request-User`, `-Groups` and
`-Preferred-Username`. `proxy_set_header` replaces whatever the client sent:

```nginx
server {
    listen 443 ssl;
    server_name devicehub.example.net;

    location /oauth2/ {
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header X-Auth-Request-Redirect $request_uri;
    }
    location = /oauth2/auth {
        internal;
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header Content-Length "";
        proxy_pass_request_body off;
    }

    location / {
        auth_request /oauth2/auth;
        error_page 401 = /oauth2/sign_in?rd=$scheme://$host$request_uri;
        auth_request_set $sso_user   $upstream_http_x_auth_request_user;
        auth_request_set $sso_groups $upstream_http_x_auth_request_groups;
        auth_request_set $sso_name   $upstream_http_x_auth_request_preferred_username;

        proxy_set_header Remote-User   $sso_user;
        proxy_set_header Remote-Groups $sso_groups;
        proxy_set_header Remote-Name   $sso_name;
        proxy_set_header X-DevHub-SSO  "<the secret>";
        proxy_pass http://192.168.1.10:8765;
    }
}
```

`DEVHUB_SSO_LOGOUT=https://devicehub.example.net/oauth2/sign_out`. The groups are your identity provider's (with
some, oauth2-proxy needs a scope or `--oidc-groups-claim` to pass them on).

### Authentik

Authentik's proxy outpost, in forward auth mode, passes `X-authentik-username`, `X-authentik-groups` (separated by
`|`) and `X-authentik-name`. Set up your proxy as above with its outpost in place of Authelia (and those headers in
place of `Remote-*`), then:

```bash
DEVHUB_SSO_USER_HEADER=X-authentik-username
DEVHUB_SSO_GROUPS_HEADER=X-authentik-groups
DEVHUB_SSO_NAME_HEADER=X-authentik-name
DEVHUB_SSO_GROUP=authentik Admins
DEVHUB_SSO_LOGOUT=https://devicehub.example.net/outpost.goauthentik.io/sign_out
```

## Checking it

- A browser at `http://<server>:8765` is redirected to the signed-in address, which asks you to sign in.
- Signed in as a member of the group, the page opens with no DeviceHub login, and **Log out** has your name on it.
- `curl -H 'Remote-User: you' -H 'Remote-Groups: admins' http://<server>:8765/api/state` is refused (401).
- A handheld still announces itself and shows online; agents' `devhub` commands still work on the port.

`tests/sso_smoke.sh` checks all of this against a hub on localhost, with made-up proxy headers.
