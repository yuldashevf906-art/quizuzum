Testchi AI Studio - Serious Motion UI

Ishlatish:
1) .env ichida GROQ_API_KEY borligini tekshiring.
2) VS Code’da main.py ochib Run bosing yoki terminalda: python main.py
3) http://127.0.0.1:8010 oching.

Click/Payme ulash:
- .env ichida PUBLIC_BASE_URL ni real domeningizga almashtiring.
- Payme uchun PAYME_MERCHANT_ID va PAYME_SECRET_KEY kiriting.
- Click uchun CLICK_MERCHANT_ID, CLICK_SERVICE_ID va CLICK_SECRET_KEY kiriting.
- Payme kabinet callback URL: https://sizning-domen.uz/api/payme/callback
- Click prepare URL: https://sizning-domen.uz/api/click/prepare
- Click complete URL: https://sizning-domen.uz/api/click/complete
- Testdan keyin provider kabinetidagi sandbox/production kalitlarini alohida tekshiring.

Payme kabinetida:
- Web kassa yarating.
- Merchant ID ni PAYME_MERCHANT_ID ga yozing.
- Test uchun TEST_KEY ni, production uchun key/parolni PAYME_SECRET_KEY ga yozing.
- Endpoint URL: https://sizning-domen.uz/api/payme/callback
- Sandbox checkout ishlatganda PAYME_CHECKOUT_URL=https://test.paycom.uz
- Production checkout ishlatganda PAYME_CHECKOUT_URL=https://checkout.paycom.uz
- Payme account field: order_id. Checkout link ichida ac.order_id avtomatik yuboriladi.

Telegram Stars:
- BotFather dan bot token oling va TELEGRAM_BOT_TOKEN ga yozing.
- Stars tariflarini .env ichida sozlang: TELEGRAM_STARS_1, TELEGRAM_STARS_7, TELEGRAM_STARS_30, TELEGRAM_STARS_TEACHER.
- Server real HTTPS domen bilan ishlashi kerak: PUBLIC_BASE_URL=https://sizning-domen.uz
- Webhook ulash uchun server ishga tushgandan keyin POST yuboring: https://sizning-domen.uz/api/telegram/set-webhook
- Telegram Stars invoice currency: XTR. Provider token kerak emas.

Firebase + Cloud Run deploy:
1) Firebase project yarating va Blaze billing yoqing.
2) Google Cloud CLI bilan login qiling:
   gcloud auth login
   gcloud config set project FIREBASE_PROJECT_ID
3) Cloud Run backend deploy:
   gcloud run deploy quizuzum --source . --region us-central1 --allow-unauthenticated
4) Cloud Run URL chiqqandan keyin envlarni qo'ying:
   gcloud run services update quizuzum --region us-central1 --set-env-vars APP_NAME=QuizUzum,PUBLIC_BASE_URL=https://FIREBASE_DOMAIN,TELEGRAM_WEBAPP_URL=https://FIREBASE_DOMAIN,GROQ_MODEL=llama-3.3-70b-versatile,GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct,GROQ_BASE_URL=https://api.groq.com/openai/v1,INITIAL_CREDITS=30,MAX_CONTEXT_CHARS=14000,REQUEST_TIMEOUT=90,TELEGRAM_STARS_1=15,TELEGRAM_STARS_7=60,TELEGRAM_STARS_30=110,TELEGRAM_STARS_TEACHER=160
5) Maxfiy kalitlarni Cloud Run Environment variables ichida alohida qo'ying:
   GROQ_API_KEY
   TELEGRAM_BOT_TOKEN
6) Firebase Hosting deploy:
   firebase init hosting
   firebase deploy --only hosting
7) Webhook:
   https://FIREBASE_DOMAIN/api/telegram/set-webhook

Yangi interfeys:
- Chap studio navigatsiya
- PDF, mavzu va kartochka generatorlari bitta workspace ichida
- Motion background va professional result sahifa
- Mobile uchun bottom navigation
