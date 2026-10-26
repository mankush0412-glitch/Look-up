# Razorpay Payment — No Webhook / No Callback URL

**Payment verification ke liye koi public URL, webhook, Cloudflare tunnel ya redirect nahi chahiye.**

## Setup

1. Dependencies: `python3 -m pip install -r requirements.txt`
2. `.env` mein existing `BOT_TOKEN`, `API_URL`, `API_KEY`, `ADMIN_IDS`, `MONGO_URI` bharein. `API_URL` aur `API_KEY` **lookup service** ke liye hain, payment verification ke liye nahi.
3. Razorpay ki credentials private environment mein dono set karein:

   ```env
   RAZORPAY_KEY_ID=rzp_live_REPLACE_WITH_YOUR_ID
   RAZORPAY_KEY_SECRET=REPLACE_WITH_YOUR_SECRET
   AUTO_CLOUDFLARE_TUNNEL=false
   ```

   Ya bot admin ke private DM mein `/setting` → **Payment Settings** → **Set Key ID / Key Secret** use karein. Dono env vars defined hain to env values preferred hoti hain. DB mein saved secret encrypted hota hai; us route par `APP_ENCRYPTION_KEY` required hai.

4. `/setting` → **Payment Settings** → **Test Razorpay** chalayein. Isme **UPI QR API** aur **Payment Links API** ke access alag-alag check honge. Razorpay mode select karein. Dono checkout options chalane ke liye Razorpay account mein dono API products enabled hone chahiye.
5. `python3 bot.py` se run karein. Yeh Telegram **long polling** chalata hai aur Razorpay API par **outbound requests** karta hai, public webhook port ya tunnel required nahi.
6. Public lookup API ko externally expose na karna ho to `ENABLE_PUBLIC_API=false` rakh sakte hain; **lekin** iss case mein paid lookup-API plan users public endpoints access nahi kar paayenge. Payment process is setting se independent hai.

## How verification works

- User plan/amount select karke **📱 Pay with UPI QR** ya **🔗 Pay with Payment Link** choose karta hai.
- QR option mein bot exact-amount, single-use QR banata hai (Razorpay's `POST /v1/payments/qr_codes`).
- Worker QR metadata check karta hai (`GET /v1/payments/qr_codes/{id}`).
- Us **particular QR** par aayi payment list fetch karta hai (`GET /v1/payments/qr_codes/{id}/payments`).
- Listed payment ID ko direct payment API se confirm karta hai (`GET /v1/payments/{id}`).
- Payment Link option mein bot unique `reference_id` aur notes ke saath Razorpay `POST /v1/payment_links` se HTTPS checkout link banata hai. Partial payment off hoti hai. Bot user ko **Pay Now** button deta hai.
- Bot `GET /v1/payment_links/{id}` se stored link ID, reference ID, user ID, currency, exact paid amount aur embedded captured payment ID verify karta hai; phir `GET /v1/payments/{id}` se independently confirm karta hai. Link ka `paid` status alone kabhi enough nahi hai.
- Sirf `captured` payment, INR, exact paise, expected QR/link/plan and no amount refunded par activate hota hai.
- `razorpay_payment_id` ka MongoDB unique index ek payment ko multiple orders mein reuse hone se rokta hai; plan/credit activation order-idempotent hai.
- Bot automatic worker (approx. har 20s) aur **Check Payment** Telegram button se check karta hai.
- Expired QR/link ko in-flight bank settlement ke liye 2h reconciliation grace milta hai. Delayed gateway/API failures par activation **nahi** hota jab tak confirmed paid status na mil jaye.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Tests live network, credentials, MongoDB ya webhook ke bina mocked response par chalte hain. **Actual ₹ payment, Razorpay account access, production deployment aur end-to-end settlement test yahan nahi hua hai.** Pehle test keys se credentials/API access check karein, phir controlled live test karein. Test-mode payments actual settlement nahi hote.

**Security:** Secret ko Git, Telegram group, code, screenshots, webhook URL ya public log mein na daalein. Existing public API domain sirf hosted lookup endpoints ke liye hai; payment verification ke liye nahi.

## Payment Link vs webhook URL

**Payment Link** ek Razorpay-hosted HTTPS URL hai jo buyer ko payment karne ke liye diya jata hai. **Webhook/callback URL** ek merchant server endpoint hai; yahan kisi webhook/callback ka istemal nahi kiya gaya. Link checkout Razorpay ki site par hota hai, aur verification bot ke authenticated outbound API polling se hoti hai.

Link default 60 minute mein expire hota hai; QR default 30 minute. User ko aapka Telegram bot running rakhna hoga, MongoDB reachable hona chahiye aur Razorpay credentials valid honi chahiye.
