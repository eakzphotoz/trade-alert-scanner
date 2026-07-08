# คู่มือตั้งค่าระบบแจ้งเตือน AI TradeScanner (Telegram + GitHub Actions)

ทำตามลำดับนี้ทีละขั้น ใช้เวลารวมประมาณ 15-20 นาที

---

## ขั้นที่ 0: เตรียมของ

- ✅ มีแอป Telegram ในมือถือ/คอม (ล็อกอินไว้แล้ว)
- ✅ มีบัญชี GitHub (ถ้ายังไม่มี สมัครฟรีที่ https://github.com/signup)
- ✅ มี Git ติดตั้งในเครื่อง เช็คด้วยคำสั่งนี้ใน PowerShell:

```powershell
git --version
```

ถ้าขึ้น error "ไม่รู้จักคำสั่ง" ให้โหลด Git จาก https://git-scm.com/download/win มาติดตั้งก่อน

---

## ขั้นที่ 1: สร้าง Telegram Bot

1. เปิด Telegram ค้นหา `@BotFather` (มีเครื่องหมายถูกสีฟ้า ของจริง)
2. พิมพ์คุยกับมันว่า:
   ```
   /newbot
   ```
3. มันจะถามชื่อบอท (ตั้งอะไรก็ได้ เช่น `Eakz Trade Alert Bot`)
4. มันจะถาม username ของบอท (ต้องลงท้ายด้วย `bot` เช่น `eakz_trade_alert_bot`)
5. เสร็จแล้วมันจะส่งข้อความกลับมาแบบนี้:
   ```
   Use this token to access the HTTP API:
   1234567890:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   **ก็อปเก็บไว้** — นี่คือ `TELEGRAM_BOT_TOKEN` ของพี่

---

## ขั้นที่ 2: หา Chat ID ของพี่

1. ใน Telegram ค้นหา `@userinfobot`
2. กด Start คุยกับมัน
3. มันจะตอบกลับมาทันที มีบรรทัด `Id: 123456789` — **ก็อปเลขนี้เก็บไว้** นี่คือ `TELEGRAM_CHAT_ID`

---

## ขั้นที่ 3: ทดสอบว่า Bot ใช้ได้จริง (ทำก่อน ค่อยไปต่อ)

เปิด PowerShell แล้วรัน (แทนที่ `<TOKEN>` และ `<CHAT_ID>` ด้วยของพี่จริงๆ):

```powershell
$token = "<TOKEN>"
$chatId = "<CHAT_ID>"
Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body @{chat_id=$chatId; text="ทดสอบระบบแจ้งเตือน ✅"}
```

ถ้าตั้งค่าถูก จะมีข้อความ "ทดสอบระบบแจ้งเตือน ✅" เด้งเข้า Telegram ทันที — ถ้ายังไม่เด้ง อย่าเพิ่งไปขั้นต่อไป ให้เช็ค token/chat id อีกรอบก่อน

---

## ขั้นที่ 4: สร้าง GitHub Repository

1. ไปที่ https://github.com/new
2. ตั้งชื่อ repo เช่น `trade-alert-scanner`
3. เลือก **Public** (แนะนำ เพราะได้โควตา GitHub Actions ฟรีไม่จำกัด — โค้ดไม่มี API key ฝังอยู่ในไฟล์เลย ปลอดภัย)
4. ไม่ต้องติ๊ก "Add a README file"
5. กด **Create repository**
6. หน้าที่ขึ้นมา ก็อป URL ของ repo ไว้ (รูปแบบ `https://github.com/ชื่อพี่/trade-alert-scanner.git`)

---

## ขั้นที่ 5: จัดไฟล์ในเครื่องให้ถูกโครงสร้าง

สร้างโฟลเดอร์ใหม่ แล้ววางไฟล์ที่ผมส่งให้ตามนี้ (โครงสร้างต้องตรงเป๊ะ):

```
trade-alert-scanner/
├── alert_scanner.py
├── alert_watchlist.json
├── rating_engine.py
├── requirements.txt          👈 ต้องสร้างเพิ่ม (ดูขั้นตอนด้านล่าง)
└── .github/
    └── workflows/
        └── alert_scanner.yml
```

สร้างไฟล์ `requirements.txt` ใหม่ (คลิกขวาในโฟลเดอร์ > New File) ใส่เนื้อหานี้:

```
yfinance
pandas
requests
```

> 💡 หมายเหตุ: `app_ai.py` (ตัวแอป Streamlit หลัก) **ไม่ต้อง**เอาใส่ repo นี้ก็ได้ เพราะ repo นี้มีไว้แค่รันสคริปต์แจ้งเตือนพื้นหลังเท่านั้น พี่เก็บ `app_ai.py` ไว้รันแยกที่เครื่อง/deploy ที่อื่นตามเดิม

---

## ขั้นที่ 6: Push ไฟล์ขึ้น GitHub

เปิด PowerShell ที่โฟลเดอร์ `trade-alert-scanner` แล้วรันทีละบรรทัด:

```powershell
cd path\ไปที่\โฟลเดอร์\trade-alert-scanner

git init
git add .
git commit -m "เริ่มต้นระบบแจ้งเตือนหุ้น"
git branch -M main
git remote add origin https://github.com/ชื่อพี่/trade-alert-scanner.git
git push -u origin main
```

ถ้าเป็นครั้งแรกที่ใช้ Git จะมีหน้าต่างเด้งขึ้นมาให้ล็อกอิน GitHub — ล็อกอินให้เรียบร้อย

เสร็จแล้วรีเฟรชหน้า GitHub repo ดู ควรเห็นไฟล์ทั้งหมดอยู่ครบ

---

## ขั้นที่ 7: ตั้งค่า Secrets ใน GitHub

1. ในหน้า repo บน GitHub ไปที่ **Settings** (แท็บบนสุด)
2. เมนูซ้าย เลือก **Secrets and variables** → **Actions**
3. กด **New repository secret**
4. เพิ่มทีละตัว:
   - Name: `TELEGRAM_BOT_TOKEN` / Value: (token จากขั้นที่ 1)
   - Name: `TELEGRAM_CHAT_ID` / Value: (chat id จากขั้นที่ 2)
5. กด **Add secret** ทั้งสองตัว

---

## ขั้นที่ 8: ทดสอบรัน Workflow ด้วยมือ

1. ไปที่แท็บ **Actions** บนสุดของหน้า repo
2. ถ้าเจอ popup ให้กด **"I understand my workflows, go ahead and enable them"**
3. เลือก workflow ชื่อ **Stock Alert Scanner** ทางซ้าย
4. กดปุ่ม **Run workflow** (มุมขวา) → กด **Run workflow** สีเขียวยืนยัน
5. รอประมาณ 30-60 วินาที รีเฟรชหน้า จะเห็นสถานะรัน (วงกลมเหลือง = กำลังรัน, ✅ = สำเร็จ, ❌ = พัง)
6. คลิกเข้าไปดู log ได้ว่าสแกนอะไรไปบ้าง เจอเรตติ้งอะไร

ถ้าตอนนี้บังเอิญมีหุ้นในลิสต์ที่เรตติ้งเป็น Strong Buy/Strong Sell พอดี จะมีข้อความเด้งเข้า Telegram ให้เห็นเลย (แต่ถ้าไม่มี ก็ไม่ต้องตกใจ ปกติมาก เพราะระบบแจ้งเฉพาะตอนเจอสัญญาณแรงจริงๆ)

---

## ขั้นที่ 9: ปล่อยให้ทำงานอัตโนมัติ

ไม่ต้องทำอะไรต่อแล้วครับ ระบบจะรันเองอัตโนมัติตามตารางเวลาใน `alert_scanner.yml` (ทุก 30 นาที ช่วงตลาดหุ้นไทย+สหรัฐฯ เปิด วันจันทร์-ศุกร์)

---

## การดูแลต่อเนื่อง

- **แก้ watchlist**: เปิด `alert_watchlist.json` ในเว็บ GitHub (คลิกไฟล์ > ปากกาแก้ไข) เพิ่ม/ลบ ticker ได้เลย commit แล้วรอบถัดไปจะใช้ลิสต์ใหม่ทันที
- **ปรับความถี่การแจ้งเตือน**: แก้บรรทัด `ALERT_LEVELS` ใน `alert_scanner.yml` เช่นเปลี่ยนเป็น `'STRONG_BUY,BUY,SELL,STRONG_SELL'` ถ้าอยากให้แจ้งไวขึ้น (แต่จะถี่ขึ้นด้วย)
- **ปรับตารางเวลา**: แก้บรรทัด `cron:` ใน `alert_scanner.yml` (เปลี่ยน `*/30` เป็น `*/15` ถ้าอยากถี่ขึ้น เพราะเป็น public repo ไม่ต้องกังวลโควตา)
- **เช็คว่ายังทำงานอยู่ไหม**: เข้าแท็บ Actions เป็นระยะ ดูว่ามีรันสำเร็จ (✅) ต่อเนื่องไหม

---

⚠️ ระบบนี้แจ้งเตือนจากการวิเคราะห์เชิงเทคนิคด้วยสูตรคำนวณเท่านั้น ไม่ใช่คำแนะนำการลงทุน โปรดใช้วิจารณญาณของตัวเองประกอบการตัดสินใจเสมอ
