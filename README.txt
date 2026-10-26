UPDATED TELEGRAM LOOKUP BOT

1. Copy .env.example to .env
2. Fill BOT_TOKEN, API_URL, API_KEY, ADMIN_IDS, MONGO_URI
3. Install:
   python3 -m pip install -r requirements.txt
4. Run:
   python3 bot.py

Credit policy:
- New user starts with 5 credits
- Successful lookup: 1 credit
- Not found: 1 credit
- Invalid input: no credit
- API/server failure: credit refunded
- Admin: unlimited

Referral policy:
- Referred new user stays at total 5 starting credits
- Referrer gets +5 for a valid new referral
- Self/duplicate referrals are blocked

Commands:
 /start
 /lookup <number>
 /bal
 /balance
 /referral
 /ping

Admin:
 /addcredit <user_id> <amount>
 /setcredit <user_id> <amount>
 /credit <user_id>

ID command:
 /id <12-digit-id> — exact ID existence/count check (does not display full personal record fields)


FORCE JOIN
-----------
Admin commands:
 /forceadd @channelusername
 /forceadd -1001234567890
 /forcelist
 /forcechannels
 /forceremove <chat_id>

Important:
- Bot ko required channel/group me admin banao.
- Private channel/group me bot ko Invite Users permission dena hoga.
- Private chats ke liye bot khud invite link generate karega.
- Public chats ke liye https://t.me/username link automatically use hoga.
- User jab tak sab configured chats join nahi karega, bot use nahi kar payega.


MANUAL CREDIT PURCHASE
----------------------
.env:
 PAYMENT_UPI_ID=yourupi@bank
 PAYMENT_ACCOUNT_NAME=Your Name
 CREDIT_PACKAGES=50:49,120:99,300:199

User:
 /buycredits
 or tap Buy Credits in the main menu.

User selects a package and sends screenshot/document or UTR.
Admins receive Approve / Reject buttons.
Approve = credits are automatically added once.
Reject = no credits are added.
User receives the final status message.

Admin API stats: /apistats or /setting -> API Stats.
Automatic plan reminders: API 3 days / 1 day / expired, and 24h bot pass expired.
