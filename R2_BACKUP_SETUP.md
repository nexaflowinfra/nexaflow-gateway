# Cloudflare R2 Offsite Backup Setup

Use this after local Railway volume backups are working. R2 keeps a copy outside Railway, which is the safer disaster recovery layer.

## 1. Create R2 Bucket

1. Open Cloudflare Dashboard.
2. Go to `R2 Object Storage`.
3. Create a bucket, for example:

```text
nexaflow-backups
```

## 2. Create R2 API Token

1. In R2, open `Manage R2 API Tokens`.
2. Create a token with read/write access to the backup bucket.
3. Copy:

```text
Access Key ID
Secret Access Key
Account ID
```

Keep the secret private.

## 3. Set Railway Variables

Set these variables in Railway for the `nexaflow-gateway` service:

```env
S3_BACKUP_ENDPOINT_URL=https://<cloudflare-account-id>.r2.cloudflarestorage.com
S3_BACKUP_BUCKET=nexaflow-backups
S3_BACKUP_ACCESS_KEY_ID=<r2-access-key-id>
S3_BACKUP_SECRET_ACCESS_KEY=<r2-secret-access-key>
S3_BACKUP_REGION=auto
S3_BACKUP_PREFIX=production
```

Do not paste these secrets into chat.

## 4. Test Without Uploading Customer Data

Open:

```text
https://api.nexaflowinfra.com/admin/dashboard
```

Use your admin key, then click:

```text
Test Offsite
```

This uploads a tiny probe file only. It does not upload the customer database.

## 5. Create A Real Backup

After `Test Offsite` returns `uploaded`, click:

```text
Create Backup
```

The backup response should show:

```text
offsite.status = uploaded
```

Automatic scheduled backups will use the same R2 configuration.
