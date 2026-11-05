# Test it yourself

Two runs. The **rehearsal** checks the whole chat-driven flow on your own computer in about ten minutes, with nothing to sign up for. The **customer run** is what a customer does: a real adapter on a machine with a public address, a real Muse and a real service. Do the rehearsal first.

| | Rehearsal | Customer run |
| --- | --- | --- |
| You need | Python 3.12+, `curl`, a browser | Also: a [Tailscale](https://tailscale.com) account, a Google Cloud project with `gcloud` signed in (or a Linux machine you can SSH into), a Muse account, an account at the service you will connect |
| Muse | A few `curl` commands that play Muse | The real Muse |
| Where it runs | Your computer, plain HTTP on `127.0.0.1` | A VM with a public HTTPS address through Tailscale Funnel |
| It shows | A proposal, the owner's review page, approval, limits, narrowing, the audit trail | The same, with Muse's real behaviour |

The command blocks are `bash`. If your shell is `zsh`, type `bash` first.

## Get the code (both runs)

```bash
git clone https://github.com/sept-usc/muse-mcp-adapter
cd muse-mcp-adapter
python3 -m venv .venv && .venv/bin/python -m pip install .
```

Until chat setup is in `main`, add `git checkout feature/chat-setup` after the `cd`.

## The rehearsal

Chat setup is off unless the config says `"chatSetup": true`. The adapter will not talk to `127.0.0.1` or plain HTTP unless you allow it, which is why the config and the two flags below are here and are not needed on a real machine.

**1. A folder, a config, and a server to connect to.**

```bash
mkdir rehearsal && cd rehearsal
cat > serve.json <<'JSON'
{"publicUrl": "http://127.0.0.1:8123", "allowInsecureHttp": true, "allowPrivateNetworks": true,
 "chatSetup": true, "listen": {"host": "127.0.0.1", "port": 8123}}
JSON
adapter="../.venv/bin/muse-mcp-adapter --database adapter.db --config serve.json --allow-http --allow-private-network"
../.venv/bin/python ../examples/sample_mcp_server.py > sample.log 2>&1 &
```

**2. Turn chat setup on and start the adapter.** The first command prints the connector to give Muse, its token and your owner code, once. It is saved in `enable.json` for this rehearsal.

```bash
$adapter chat-setup-enable | tee enable.json
$adapter serve > adapter.log 2>&1 &
```

**3. Play Muse.** `muse` calls the setup connector the way Muse does.

```bash
SLUG=$(python3 -c "import json,re;print(re.search(r'/c/([^/]+)/',json.load(open('enable.json'))['connectorUrl']).group(1))")
TOKEN=$(python3 -c "import json;print(json.load(open('enable.json'))['token'])")
muse() { curl -sS -X POST "http://127.0.0.1:8123/c/$SLUG/operations/$1" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$2"; }

muse propose_connection '{"endpoint":"http://127.0.0.1:9101/mcp","readOnly":true,"userWords":"Connect the sample tracker, read only."}'
```

The answer has a `reviewLink` and the state `waiting_for_owner`. Nothing is exposed and the sample server has not been contacted.

**4. Be the owner.** In your browser, open the `reviewLink`.

1. Type the `ownerCode` from `enable.json`.
2. The page says Muse supplied this address and it is not in the adapter's list, and asks how it signs in. **No sign-in** is already chosen. Press **Continue**. (The adapter contacts the sample server only now.)
3. Read the page. Your words are in a box marked unverified. Below is each operation, whether it only reads or changes data, and a line such as *Asked for: 7. Would be available: 7.* Leave `find_issue` and `search_issues` ticked, untick one other (for example `export_all`), then press **Approve**.
4. The page shows the connector address and its token, once. Copy both.

**5. Play Muse with the new connector.**

```bash
ADDRESS=...   # the connector address from the page
CTOKEN=...    # the connector token from the page
CSLUG=$(echo "$ADDRESS" | sed -E 's#.*/c/([^/]+)/.*#\1#')
call() { curl -sS -w '  -> HTTP %{http_code}\n' -X POST "http://127.0.0.1:8123/c/$CSLUG/operations/$1" -H "Authorization: Bearer $CTOKEN" -H 'Content-Type: application/json' -d "$2"; }

call find_issue '{"issue_id":"ISSUE-1"}'    # a read you approved: 200
call create_issue '{"title":"x"}'           # a write nobody approved: 404, it was never offered
```

**6. Muse can make access smaller on its own.**

```bash
CID=$(muse list_connections '{}' | python3 -c "import json,sys;print(json.load(sys.stdin)['structuredContent']['connections'][0]['connectionId'])")

muse turn_off_operations "{\"connectionId\":\"$CID\",\"operations\":[\"search_issues\"]}"
call search_issues '{}'                     # 404: gone at once, no page needed

muse limit_arguments "{\"connectionId\":\"$CID\",\"limitArguments\":[{\"operation\":\"find_issue\",\"argument\":\"issue_id\",\"allowedValues\":[\"ISSUE-1\"]}]}"
call find_issue '{"issue_id":"ISSUE-1"}'    # 200
call find_issue '{"issue_id":"ISSUE-2"}'    # 400: limited by the owner to ISSUE-1
```

**7. Anything that gives Muse more needs you.**

```bash
muse propose_change "{\"connectionId\":\"$CID\",\"userWords\":\"Also let it create issues.\",\"alsoAllow\":[\"create_issue\"]}"
```

Open the new `reviewLink`. The page says what stays live and what is new (one operation that changes data). Approve it, then `call create_issue '{"title":"x"}'` answers 200 on the same address and token. Until you approve, it is 404.

**8. Read the record.** Every step is in the audit, and each row carries a hash of the row before it.

```bash
$adapter proposals --all
$adapter audit-verify
$adapter explain-call --request-id REQUEST_ID
```

Every answer from `call` carries a `requestId`. `explain-call` follows a call to the catalog it ran under. When that catalog came from an approval it goes on to the proposal and the words you approved; when Muse narrowed the connection after that, it says so and shows the change.

**9. Stop.**

```bash
kill $(jobs -p)
cd .. && rm -rf rehearsal
```

## The customer run

This is what a customer does. It creates billable resources (a small VM on Google Cloud) and asks before it does.

```bash
.venv/bin/muse-vm doctor
.venv/bin/muse-vm setup --chat-setup
```

`doctor` says what would stop a deployment before anything is created. `setup` asks where to run the adapter (a new Google Cloud VM, or a Linux machine of yours with `--ssh user@host`), opens up to two Tailscale links for you to approve, builds and starts the adapter, and checks it from outside and inside. It takes 5 to 10 minutes and is safe to run again. [Getting started](getting-started.md) explains each step. `--chat-setup` also turns on adding servers by chat, so it skips the question about which servers to connect.

At the end it prints, once each: **a sentence to give Muse with a token**, and **your owner code**. Keep both. To use a password you choose instead, run `.venv/bin/muse-vm chat-setup --set-owner-password`. The owner code or password goes only on your adapter's own review pages, never into Muse.

**Add the one connector by hand.** In a new Muse chat send the sentence, click **Connect**, and paste the token into Muse's own field. This is the only connector you add yourself.

**Then ask Muse in plain words.** Use a service you can afford to experiment with. For GitHub, make a [fine-grained token](https://github.com/settings/personal-access-tokens/new) limited to one repository, with read access to issues and pull requests.

| You say | What should happen |
| --- | --- |
| *Connect my GitHub, read only, only the repository OWNER/REPO, for the next two hours.* | Muse gives you a link. Nothing is exposed yet |
| (on your phone or computer) open the link, type the owner password, paste the GitHub token | The adapter reads what GitHub offers and freezes it. The page says Muse has not drafted a policy yet, and refuses to approve an empty one |
| *I am on the review page.* | Muse reads the details and drafts the smallest policy, with a reason for each operation |
| (reload the page) read the summary, type the owner password again, **Approve** | The page shows a connector address and token once |
| (in Muse) click **Connect** and paste that token | Muse says the connector works |
| *List the open issues in OWNER/REPO.* | Real data |
| *List the open issues in SOMEONE/ELSE.* | Refused: the value is not the one you limited it to |
| *Comment "hello" on issue 1.* | Muse cannot: no operation was approved that changes data |
| *Also let it comment on issues in that repository.* | A new link. The page shows one new operation that changes data. Approve it, and Muse can comment (it asks you first) |
| *Stop it commenting.* | Switched off at once, with no page |
| *Approve that request yourself.* | Muse cannot. Nothing it has can approve |
| *End its access in one hour.* | Applied at once. Access is given in whole hours, so come back after the hour: calls are then refused, with the end time in the message |

Check the page's alignment line each time: a bare "read only" counts every read the service offers as asked for, so the list can be longer than you meant. Untick what you do not want.

**Read the record on the machine.** Connect to it (for Google Cloud, `gcloud compute ssh mma-NAME --tunnel-through-iap`, where NAME is the deployment name from `muse-vm list`), then:

```bash
cd /opt/muse-adapter
sudo docker compose exec adapter muse-mcp-adapter --database /data/adapter.db --config /config/serve.json proposals --all
sudo docker compose exec adapter muse-mcp-adapter --database /data/adapter.db --config /config/serve.json audit-verify
```

[Set up by chat](chat-setup.md#the-record) has the rest of the commands.

**Clean up.** Remove the connectors from Muse, revoke the service token you made, and remove the deployment and everything it created:

```bash
.venv/bin/muse-vm down
```

## While you test

- **A link stops working.** If Muse repeats a request while your page is open, you may get a new link and the old one stops. Ask Muse for the current link.
- **A link is locked.** Five wrong owner passwords lock a link for 15 minutes. `proposal-link --proposal-id ID` prints a fresh link, but the lock stays until the 15 minutes pass.
- **"What is live changed while you were reviewing."** Muse made the connection smaller while you were reading a change. Ask for the change again.
- **Muse's own limits.** Muse's secure token card has failed to reach the adapter on some tries. A new connector name has worked.
- **What is not enforced.** The adapter does not yet ask you before each call, and limits reach tools only, not prompts or address templates. The review page lists these under what is not enforced.
