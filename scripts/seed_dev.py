"""Development seed script.

Creates or updates the "demo" textile manufacturer tenant with:
- 20 products (ko'rpa, yostiq, namatrasnik, topper, choyshab variants)
- 15 FAQ entries (Uzbek + Russian)
- Brand voice knowledge doc
- Agent records (orchestrator, conversation, content, lead_qualifier)
- Telegram bot token (from TELEGRAM_BOT_TOKEN env var, if set)

Usage:
    python scripts/seed_dev.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import settings
from app.db.models.agent import Agent
from app.db.models.conversation import Conversation
from app.db.models.knowledge_doc import KnowledgeDoc
from app.db.models.product import Product
from app.db.models.tenant import Tenant
from app.db.session import get_admin_session

_fernet = Fernet(settings.encryption_key.encode())

TENANT_SLUG = "demo"

PRODUCTS = [
    # sku, name_uz, name_ru, desc, price_uzs
    ("KORPA-150-W", "Ko'rpa 150x200 (Qish)", "Одеяло зимнее 150x200", "Qish uchun issiq ko'rpa, 100% paxta.", 320_000),
    ("KORPA-150-S", "Ko'rpa 150x200 (Yoz)", "Одеяло летнее 150x200", "Yoz uchun yengil ko'rpa.", 220_000),
    ("KORPA-175-W", "Ko'rpa 175x215 (Qish)", "Одеяло зимнее 175x215", "Ikki kishilik qish ko'rpasi.", 420_000),
    ("KORPA-175-S", "Ko'rpa 175x215 (Yoz)", "Одеяло летнее 175x215", "Ikki kishilik yoz ko'rpasi.", 290_000),
    ("KORPA-200-W", "Ko'rpa 200x220 (Qish)", "Одеяло зимнее 200x220", "King size qish ko'rpasi.", 520_000),
    ("YOSTIQ-50",   "Yostiq 50x70",           "Подушка 50x70",         "Yumshoq tola yostiq.", 95_000),
    ("YOSTIQ-70",   "Yostiq 70x70",           "Подушка 70x70",         "Katta standart yostiq.", 120_000),
    ("YOSTIQ-ORG",  "Organik yostiq 50x70",   "Органическая подушка",  "Organik paxta, gippoallergen.", 150_000),
    ("NAM-90",      "Namatrasnik 90x200",      "Наматрасник 90x200",   "Nozik ko'rpachali himoya.", 180_000),
    ("NAM-160",     "Namatrasnik 160x200",     "Наматрасник 160x200",  "Ikki kishilik, suv o'tkazmaydigan.", 270_000),
    ("TOPPER-90",   "Topper 90x200 (5sm)",     "Топпер 90x200",        "5 sm qalinlikdagi topper.", 350_000),
    ("TOPPER-160",  "Topper 160x200 (5sm)",    "Топпер 160x200",       "Juft topper, ortopedik.", 490_000),
    ("CHOY-160",    "Choyshab to'plami 160",   "КПБ 160x200",          "160x200: choyshab, 2 yostiq qop.", 380_000),
    ("CHOY-200",    "Choyshab to'plami 200",   "КПБ 200x220",          "King size to'plam.", 480_000),
    ("CHOY-90",     "Choyshab to'plami 90",    "КПБ 90x200",           "Bir kishilik to'plam.", 260_000),
    ("KORPA-KIDS",  "Bolalar ko'rpasi",         "Детское одеяло",       "2-10 yosh uchun, 110x140.", 190_000),
    ("YOSTIQ-KIDS", "Bolalar yostig'i 40x60",  "Детская подушка",      "Kichik yostiq, tola.", 70_000),
    ("TOPPER-180",  "Topper 180x200",           "Топпер 180x200",       "Keng topper, 7 sm.", 560_000),
    ("CHOY-PREM",   "Premium choyshab (satin)", "Сатин КПБ 160",        "Satin material, 220 TC.", 620_000),
    ("KORPA-BMBK",  "Bambuk ko'rpa 175x215",   "Бамбуковое одеяло",    "Ekologik bambuk to'ldirish.", 480_000),
]

FAQS: list[tuple[str, str]] = [
    # (title, content) — all bilingual
    (
        "Yetkazib berish muddati / Сроки доставки",
        "Toshkent bo'yicha 1-2 kun, viloyatlarga 3-5 kun. / По Ташкенту 1-2 дня, по регионам 3-5 дней.",
    ),
    (
        "To'lov usullari / Способы оплаты",
        "Naqd pul, Click, Payme, bank o'tkazma. / Наличные, Click, Payme, банковский перевод.",
    ),
    (
        "Qaytarish shartlari / Условия возврата",
        "Tovar ochilmagan bo'lsa 14 kun ichida qaytarish mumkin. / Возврат в течение 14 дней при неповреждённой упаковке.",
    ),
    (
        "Ko'rpa o'lchamlari / Размеры одеял",
        "150x200 (1 kishi), 175x215 (2 kishi), 200x220 (King). / 150x200 (одинарное), 175x215 (двойное), 200x220 (King).",
    ),
    (
        "Narxlar / Цены",
        "Narxlar 70 000 so'mdan boshlanadi. To'liq narx ro'yxati uchun kataloggga murojaat qiling. / Цены от 70 000 сум. Полный прайс-лист по запросу.",
    ),
    (
        "Ulgurji xarid / Оптовые закупки",
        "10 dona va undan ko'p buyurtma uchun 10-15% chegirma beriladi. / Скидка 10-15% при заказе от 10 штук.",
    ),
    (
        "Yuvish yo'riqnomalari / Инструкции по стирке",
        "30°C suvda yuving, qo'lda yoki nozik rejimda. Quritgichda qo'llaming! / Стирка при 30°C, вручную или на деликатном режиме. Не в сушилку!",
    ),
    (
        "Yetkazib berish narxi / Стоимость доставки",
        "100 000 so'mdan yuqori buyurtmalarga bepul yetkazib berish. / Бесплатная доставка при заказе от 100 000 сум.",
    ),
    (
        "Kafolat / Гарантия",
        "Mahsulot sifatiga 1 yil kafolat. / 1 год гарантии на качество продукции.",
    ),
    (
        "Rang va dizayn / Цвета и дизайны",
        "20+ rang va naqsh mavjud. Rasmlar uchun Instagram sahifamizga o'ting. / Более 20 цветов и принтов. Фото в нашем Instagram.",
    ),
    (
        "Organik mahsulotlar / Органическая продукция",
        "Organik paxta va bambuk to'ldirish bilan mahsulotlar mavjud. / Есть изделия с органическим хлопком и бамбуковым наполнением.",
    ),
    (
        "Bolalar uchun xavfsizlik / Безопасность для детей",
        "Bolalar seriyasi gippoallergen materiallardan tayyorlangan. / Детская серия из гипоаллергенных материалов.",
    ),
    (
        "Buyurtma berish / Как сделать заказ",
        "Telegram yoki Instagram orqali mahsulot nomini va telefon raqamingizni yuboring. / Отправьте название товара и номер телефона через Telegram или Instagram.",
    ),
    (
        "Topper nima / Что такое топпер",
        "Topper — matras ustiga qo'yiladigan yupqa ko'rpacha bo'lib, qulaylikni oshiradi. / Топпер — тонкая мягкая накладка на матрас для комфорта.",
    ),
    (
        "Namatrasnik va topper farqi / Разница наматрасника и топпера",
        "Namatrasnik — himoya, topper — qo'shimcha yumshoqlik. / Наматрасник защищает матрас, топпер добавляет мягкость.",
    ),
]

BRAND_VOICE = """
Bu brend haqida:
Biz Toshkentda joylashgan to'shak va to'qimachilik mahsulotlari ishlab chiqaruvchisimiz.
Asosiy mahsulotlar: ko'rpa, yostiq, namatrasnik, topper, choyshab to'plamlari.
Mijozlarimiz — sifatli va qulay uy to'qimachiligi mahsulotlarini izlayotgan o'zbek oilalari.

Brand ovozi: Iliq, samimiy, profesional. Uyning qulayligini nishonlaymiz.
Til: Asosan o'zbek tilida muloqot, rus tilida ham javob bera olamiz.
Raqobatchilar haqida hech qachon gapirmaymiz.
Narxlarni faqat katalogdan kelgan ma'lumot asosida aytamiz.

О бренде (по-русски):
Мы производитель постельных принадлежностей и текстиля в Ташкенте.
Основные продукты: одеяла, подушки, наматрасники, топперы, КПБ.
Клиенты — узбекские семьи, ищущие качественный домашний текстиль.
Голос бренда: тёплый, искренний, профессиональный.
"""

AGENT_CONFIGS = [
    {
        "type": "orchestrator",
        "name": "Orchestrator",
        "autonomy_level": 2,
        "config_json": {"model": "claude-opus-4-7"},
    },
    {
        "type": "conversation",
        "name": "Conversation Agent",
        "autonomy_level": 2,
        "config_json": {"model": "claude-sonnet-4-6"},
    },
    {
        "type": "content",
        "name": "Content Agent",
        "autonomy_level": 1,
        "config_json": {"model": "claude-sonnet-4-6"},
    },
    {
        "type": "lead_qualifier",
        "name": "Lead Qualifier",
        "autonomy_level": 2,
        "config_json": {"model": "claude-haiku-4-5"},
    },
    {
        "type": "comment_responder",
        "name": "Comment Responder",
        "autonomy_level": 2,
        "config_json": {
            "model": "claude-haiku-4-5",
            "blocked_keywords": ["spam", "click here", "t.co", "bit.ly"],
            "competitor_brands": [],
        },
    },
]

INSTAGRAM_POSTS = [
    (
        "instagram_post",
        "Yangi Ko'rpa to'plami — 2024 kuz kolleksiyasi!",
        (
            "Kuz keldi — uyingizni iliq va qulay qiling! "
            "Yangi 2024 kuz kolleksiyamizdagi ko'rpalar 100% paxtadan tayyorlangan. "
            "150x200 dan 200x220 gacha o'lchamlar mavjud. "
            "Narxlar 220 000 so'mdan boshlanadi. "
            "Buyurtma uchun DM yozing yoki Telegram kanalimizga o'ting. "
            "#korpa #uyuchun #ozbekistan #tekstil"
        ),
    ),
    (
        "instagram_post",
        "Topper aksiyasi — 30% chegirma!",
        (
            "Bu hafta TOPPER-90 va TOPPER-160 modellarimizda 30% chegirma! "
            "Matras ustiga qo'yiladigan topper uxlash sifatini sezilarli oshiradi. "
            "Ortopedik ko'pik + paxta qoplama. "
            "Aksiya 3 kun davom etadi — ulguring! "
            "Narx va buyurtma uchun DM. "
            "#topper #matras #chegirma #aksiya"
        ),
    ),
    (
        "instagram_post",
        "Organik yostiq — Gippoallergen",
        (
            "Allergiyangiz bormi? Bizning organik paxta yostiqlarimiz — siz uchun! "
            "YOSTIQ-ORG modeli 50x70 o'lchamda, 100% organik, sertifikatlangan. "
            "Bolalar va sezgir terili odamlar uchun ideal. "
            "Narxi: 150 000 so'm. Yetkazib berish — bepul (100 000 so'mdan). "
            "#organik #yostiq #gippoallergen #bolalar"
        ),
    ),
    (
        "instagram_post",
        "Ulgurji xarid — 10+ dona",
        (
            "Mehmonxona, hostel yoki ofis uchun ko'rpa-yostiq kerakmi? "
            "10 dona va undan ortiq buyurtmalarda 10–15% chegirma beramiz. "
            "Barcha o'lchamlar va modellar mavjud. "
            "Hisob-kitob va narx ro'yxati uchun DM yoki Telegram: @AlphaTextilUZ. "
            "#ulgurji #opt #mehmonxona #tekstil #toshkent"
        ),
    ),
    (
        "instagram_post",
        "Choyshab to'plami — Satin Premium",
        (
            "Satin materialdan tayyorlangan premium choyshab to'plami! "
            "CHOY-PREM: 160x200 choyshab + 2 ta yostiq qop, 220 TC satin. "
            "Yumshoq, chiroyli, uzoq xizmat qiladi. "
            "Narxi: 620 000 so'm. Rang tanlash imkoniyati bor (10+ rang). "
            "Buyurtma: DM yoki +998 ** *** ** ** (Telegram orqali). "
            "#choyshab #satin #premium #uy #sovga"
        ),
    ),
]


async def main() -> None:
    async with get_admin_session() as session:
        # Upsert tenant
        result = await session.execute(select(Tenant).where(Tenant.slug == TENANT_SLUG))
        tenant = result.scalar_one_or_none()

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or settings.telegram_bot_token
        encrypted_token = _fernet.encrypt(bot_token.encode()) if bot_token else None
        webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "dev-webhook-secret-1234")
        encrypted_secret = _fernet.encrypt(webhook_secret.encode())

        if tenant is None:
            tenant = Tenant(
                name="Alpha Textile UZ",
                slug=TENANT_SLUG,
                plan="starter",
                billing_mode="managed",
                status="trial",
                telegram_bot_token_encrypted=encrypted_token,
                telegram_webhook_secret=encrypted_secret,
            )
            session.add(tenant)
            await session.flush()
            print(f"Created tenant: {tenant.id}")
        else:
            tenant.telegram_bot_token_encrypted = encrypted_token
            tenant.telegram_webhook_secret = encrypted_secret
            print(f"Updated tenant: {tenant.id}")

        tid = tenant.id

        # Upsert products (tenant_id must be set manually — no RLS here)
        for sku, name_uz, name_ru, desc, price in PRODUCTS:
            res = await session.execute(
                select(Product).where(Product.tenant_id == tid, Product.sku == sku)
            )
            prod = res.scalar_one_or_none()
            if prod is None:
                session.add(Product(
                    tenant_id=tid,
                    sku=sku,
                    name_uz=name_uz,
                    name_ru=name_ru,
                    description=desc,
                    price_uzs=price,
                    in_stock=True,
                ))
        print(f"Seeded {len(PRODUCTS)} products")

        # Upsert knowledge docs
        for title, content in FAQS:
            res = await session.execute(
                select(KnowledgeDoc).where(
                    KnowledgeDoc.tenant_id == tid,
                    KnowledgeDoc.title == title,
                )
            )
            doc = res.scalar_one_or_none()
            if doc is None:
                session.add(KnowledgeDoc(
                    tenant_id=tid,
                    type="faq",
                    title=title,
                    content=content,
                    vector_id=str(uuid.uuid4()),
                ))
        print(f"Seeded {len(FAQS)} FAQ entries")

        # Brand voice doc
        res = await session.execute(
            select(KnowledgeDoc).where(
                KnowledgeDoc.tenant_id == tid,
                KnowledgeDoc.type == "brand_voice",
            )
        )
        if res.scalar_one_or_none() is None:
            session.add(KnowledgeDoc(
                tenant_id=tid,
                type="brand_voice",
                title="Brand Voice",
                content=BRAND_VOICE,
                vector_id=str(uuid.uuid4()),
            ))
        print("Seeded brand voice doc")

        # Upsert Instagram post docs
        for doc_type, title, content in INSTAGRAM_POSTS:
            res = await session.execute(
                select(KnowledgeDoc).where(
                    KnowledgeDoc.tenant_id == tid,
                    KnowledgeDoc.title == title,
                )
            )
            if res.scalar_one_or_none() is None:
                session.add(KnowledgeDoc(
                    tenant_id=tid,
                    type=doc_type,
                    title=title,
                    content=content,
                    vector_id=str(uuid.uuid4()),
                ))
        print(f"Seeded {len(INSTAGRAM_POSTS)} Instagram post docs")

        # Upsert agents
        for cfg in AGENT_CONFIGS:
            res = await session.execute(
                select(Agent).where(Agent.tenant_id == tid, Agent.type == cfg["type"])
            )
            agent = res.scalar_one_or_none()
            if agent is None:
                session.add(Agent(
                    tenant_id=tid,
                    type=cfg["type"],
                    name=cfg["name"],
                    autonomy_level=cfg["autonomy_level"],
                    config_json=cfg["config_json"],
                    enabled=True,
                ))
        print(f"Seeded {len(AGENT_CONFIGS)} agent records")

        await session.commit()
        print("Done. Tenant slug:", TENANT_SLUG)


if __name__ == "__main__":
    asyncio.run(main())
