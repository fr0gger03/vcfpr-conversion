## Configuring Zerto Keycloak Credentials & Swagger API Access

Modern Zerto Virtual Manager Appliances (ZVMA) use **Keycloak OAuth 2.0 / OpenID Connect** for all API authentication, deprecating legacy session-based username/password logins. Generating a dedicated API Client ID and Secret in Keycloak is required to run automated scripts and interact with the Zerto REST API via Swagger.

---

### Why Keycloak Authentication is Required for Swagger

When navigating to the built-in Zerto Swagger API documentation (`https://<ZVM_IP>/zvm/docs/`), standard vCenter or administrative user credentials will not authorize API calls.

* **OAuth 2.0 Protection:** All ZVMA `/v1` endpoints require a valid JWT Bearer Token passed in the HTTP Authorization header (`Authorization: Bearer <token>`).
* **Interactive Testing in Swagger:** To execute API calls (`GET`, `POST`, `DELETE`) directly within the Swagger UI, you must first request an access token using your Keycloak Client Credentials, then paste that token into Swagger's **Authorize** modal.
* **Non-Interactive Automation:** Python scripts (such as `01_zerto_discover_and_export.py`) use the same Client ID and Secret to request short-lived bearer tokens programmatically without human intervention.

---

### Step-by-Step: Creating an API Client in Zerto Keycloak

1. **Access the Keycloak Administration Console**
* Open your browser and navigate to:
```text
https://<ZVM_IP>/auth/admin/

```


* Log in using your ZVMA system administrator credentials.


2. **Select the Zerto Realm**
* Ensure you are in the **`zerto`** realm (selectable from the top-left realm dropdown menu).


3. **Create a New OAuth Client**
* In the left navigation panel, click **Clients**.
* Click **Create** in the top-right corner.
* Configure the following basic parameters:
* **Client ID:** `zerto-python-script` (or a descriptive name of your choice)
* **Client Protocol:** `openid-connect`


* Click **Save**.


4. **Configure Access Settings**
* On the **Settings** tab for your new client, update the following fields:
* **Access Type:** Set to `confidential`
* **Service Accounts Enabled:** Toggle to `ON` (This enables the `client_credentials` grant type required for headless script authentication)
* **Direct Access Grants Enabled:** Toggle to `OFF`


* Click **Save**.


5. **Retrieve the Client Secret**
* Click the **Credentials** tab at the top of the client configuration screen.
* Locate the **Secret** field. Copy this value and store it securely.



---

### Testing Credentials in Swagger UI

1. Open Zerto Swagger UI at `https://<ZVM_IP>/zvm/docs/`.
2. Locate the `/auth/realms/zerto/protocol/openid-connect/token` endpoint.
3. Submit a `POST` request with your `client_id` and `client_secret` using `grant_type=client_credentials`.
4. Copy the returned `access_token` string.
5. Click the green **Authorize** button at the top of the Swagger page, paste the token into the **Value** box as `Bearer <your_token_here>`, and click **Authorize**. You can now execute API calls directly from the browser.