import asyncio
import io
import logging
import os
import re
import secrets
import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web
import qrcode

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("raiden-shogun")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
FEEDBACK_CHANNEL_ID = int(os.getenv("FEEDBACK_CHANNEL_ID", "0") or 0)
TRANSCRIPT_CHANNEL_ID = int(os.getenv("TRANSCRIPT_CHANNEL_ID", "0") or 0)
STORE_NAME = os.getenv("STORE_NAME", "Shogun's Garden")
DB_PATH = "raiden.db"

if not TOKEN:
    raise RuntimeError("Configure DISCORD_TOKEN no arquivo .env antes de iniciar.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
db: aiosqlite.Connection | None = None
http_session: aiohttp.ClientSession | None = None
web_runner: web.AppRunner | None = None


def money(value: Decimal | float | int) -> str:
    return f"R$ {Decimal(str(value)):.2f}".replace(".", ",")


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID or (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


async def ensure_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("""CREATE TABLE IF NOT EXISTS custom_commands (
        guild_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
        response TEXT NOT NULL, PRIMARY KEY(guild_id, name))""")
    await db.execute("""CREATE TABLE IF NOT EXISTS tickets (
        channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, opened_at TEXT NOT NULL, status TEXT NOT NULL)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
        amount TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, rating INTEGER NOT NULL, comment TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS wallets (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, coins INTEGER NOT NULL DEFAULT 0,
        last_daily TEXT, PRIMARY KEY(guild_id, user_id))""")
    await db.execute("""CREATE TABLE IF NOT EXISTS inventory (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, item TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id, user_id, item))""")
    await db.commit()


async def db_execute(query, params=()):
    assert db is not None
    await db.execute(query, params)
    await db.commit()


async def db_fetchone(query, params=()):
    assert db is not None
    cur = await db.execute(query, params)
    return await cur.fetchone()


async def db_fetchall(query, params=()):
    assert db is not None
    cur = await db.execute(query, params)
    return await cur.fetchall()


def make_embed(description: str, color: int = 0x7B2CBF, image_url: str = "") -> discord.Embed:
    embed = discord.Embed(description=description[:4000], color=color)
    embed.set_footer(text=f"{STORE_NAME} • Raiden Shogun")
    if image_url:
        if image_url.startswith("https://") or image_url.startswith("http://"):
            embed.set_image(url=image_url)
    return embed


class EmbedBuilderModal(discord.ui.Modal, title="Criar embed"):
    description = discord.ui.TextInput(
        label="Descrição (obrigatória)", style=discord.TextStyle.paragraph,
        placeholder="Escreva a mensagem que aparecerá no embed...", max_length=4000
    )
    color = discord.ui.TextInput(
        label="Cor hexadecimal (opcional)", placeholder="7B2CBF",
        required=False, max_length=6
    )
    image_url = discord.ui.TextInput(
        label="URL da imagem (opcional)", placeholder="https://...",
        required=False, max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        color_text = str(self.color.value).strip().lstrip("#")
        color = 0x7B2CBF
        if color_text:
            try:
                color = int(color_text, 16)
                if not 0 <= color <= 0xFFFFFF:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "Cor inválida. Use seis caracteres hexadecimais, como `7B2CBF`.",
                    ephemeral=True
                )
                return
        embed = make_embed(str(self.description.value), color, str(self.image_url.value).strip())
        view = PublishEmbedView(embed)
        await interaction.response.send_message(
            "Prévia do embed. Clique em **Publicar aqui** para enviar neste canal.",
            embed=embed, view=view, ephemeral=True
        )


class PublishEmbedView(discord.ui.View):
    def __init__(self, embed: discord.Embed):
        super().__init__(timeout=600)
        self.embed = embed

    @discord.ui.button(label="Publicar aqui", style=discord.ButtonStyle.success, emoji="📨")
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.channel or not interaction.guild:
            await interaction.response.send_message("Use este botão em um servidor.", ephemeral=True)
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Só a equipe autorizada pode publicar.", ephemeral=True)
            return
        await interaction.channel.send(embed=self.embed)
        await interaction.response.send_message("Embed publicado.", ephemeral=True)
        self.stop()


class TicketIntakeModal(discord.ui.Modal, title="Formulário de atendimento"):
    subject = discord.ui.TextInput(label="Assunto", max_length=100)
    details = discord.ui.TextInput(
        label="Como podemos ajudar?", style=discord.TextStyle.paragraph, max_length=1500
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Abra este formulário dentro do servidor.", ephemeral=True)
            return
        category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            try:
                category = await guild.create_category("Tickets", reason="Categoria de tickets Raiden Shogun")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Preciso da permissão **Gerenciar canais** para criar tickets.", ephemeral=True
                )
                return

        safe_name = re.sub(r"[^a-z0-9-]", "-", interaction.user.name.lower())[:18].strip("-") or "cliente"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            ),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
            )
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{safe_name}", category=category, overwrites=overwrites,
                topic=f"Ticket de {interaction.user} ({interaction.user.id})"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Não consegui criar o canal. Verifique minhas permissões.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc).isoformat()
        await db_execute(
            "INSERT INTO tickets(channel_id,guild_id,user_id,opened_at,status) VALUES(?,?,?,?,?)",
            (channel.id, guild.id, interaction.user.id, now, "open")
        )
        embed = make_embed(
            f"**Cliente:** {interaction.user.mention}\n**Assunto:** {self.subject.value}\n\n"
            f"**Detalhes:**\n{self.details.value}"
        )
        await channel.send(
            content=f"{interaction.user.mention} nossa equipe atenderá você aqui.",
            embed=embed, view=CloseTicketView()
        )
        await interaction.response.send_message(f"Seu ticket foi criado: {channel.mention}", ephemeral=True)


class OpenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="raiden:open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Use em um servidor.", ephemeral=True)
            return
        existing = await db_fetchone(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
            (interaction.guild.id, interaction.user.id)
        )
        if existing:
            channel = interaction.guild.get_channel(existing["channel_id"])
            if channel:
                await interaction.response.send_message(
                    f"Você já tem um ticket aberto: {channel.mention}", ephemeral=True
                )
                return
        await interaction.response.send_modal(TicketIntakeModal())


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="raiden:close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Este botão só funciona dentro de um ticket.", ephemeral=True)
            return
        row = await db_fetchone(
            "SELECT user_id,status FROM tickets WHERE channel_id=?", (interaction.channel.id,)
        )
        if not row or row["status"] != "open":
            await interaction.response.send_message("Este ticket não está aberto.", ephemeral=True)
            return
        if interaction.user.id != row["user_id"] and not is_admin(interaction):
            await interaction.response.send_message("Só quem abriu o ticket ou a equipe pode fechá-lo.", ephemeral=True)
            return

        await db_execute("UPDATE tickets SET status='closed' WHERE channel_id=?", (interaction.channel.id,))
        await interaction.response.send_message("Ticket encerrado. O canal será removido em 5 segundos.")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket fechado por {interaction.user}")
        except discord.HTTPException:
            pass


class FeedbackModal(discord.ui.Modal, title="Avaliar Shogun's Garden"):
    rating = discord.ui.TextInput(
        label="Nota de 1 a 5", placeholder="5", min_length=1, max_length=1
    )
    comment = discord.ui.TextInput(
        label="Comentário", style=discord.TextStyle.paragraph, max_length=1000,
        required=False, placeholder="Conte como foi sua experiência..."
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rating = int(str(self.rating.value))
            if rating < 1 or rating > 5:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("A nota precisa ser um número de 1 a 5.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("Use em um servidor.", ephemeral=True)
            return
        comment = str(self.comment.value).strip()
        await db_execute(
            "INSERT INTO feedback(guild_id,user_id,rating,comment,created_at) VALUES(?,?,?,?,?)",
            (interaction.guild.id, interaction.user.id, rating, comment, datetime.now(timezone.utc).isoformat())
        )
        channel_id = FEEDBACK_CHANNEL_ID
        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
        else:
            channel = None
        if channel and isinstance(channel, discord.TextChannel):
            embed = make_embed(
                f"**Avaliação:** {'⭐' * rating}\n**Cliente:** {interaction.user.mention}\n"
                f"**Comentário:** {comment or 'Sem comentário.'}"
            )
            await channel.send(embed=embed)
        await interaction.response.send_message("Obrigado pelo feedback! 💜", ephemeral=True)


async def create_pix_payment(user_id: int, guild_id: int, amount: Decimal):
    if not MP_ACCESS_TOKEN or not MP_ACCESS_TOKEN.startswith(("TEST-", "APP_USR-")):
        raise RuntimeError("Configure MP_ACCESS_TOKEN no arquivo .env.")
    if amount < Decimal("1.00") or amount > Decimal("10000.00"):
        raise ValueError("O valor deve ser entre R$ 1,00 e R$ 10.000,00.")
    assert http_session is not None
    external_reference = f"raiden-{guild_id}-{user_id}-{secrets.token_hex(6)}"
    payload = {
        "transaction_amount": float(amount.quantize(Decimal("0.01"))),
        "description": f"{STORE_NAME} - pedido Discord",
        "payment_method_id": "pix",
        "external_reference": external_reference,
        "payer": {"email": f"cliente{user_id}@example.com"},
        "notification_url": MP_WEBHOOK_URL or None,
        "metadata": {"discord_user_id": str(user_id), "discord_guild_id": str(guild_id)}
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": secrets.token_hex(16)
    }
    async with http_session.post(
        "https://api.mercadopago.com/v1/payments", json=payload, headers=headers
    ) as response:
        data = await response.json(content_type=None)
        if response.status not in (200, 201):
            log.warning("Mercado Pago retornou status %s", response.status)
            raise RuntimeError("O Mercado Pago recusou a cobrança. Confira as credenciais e a conta.")
    payment_id = str(data.get("id", ""))
    point = data.get("point_of_interaction", {}).get("transaction_data", {})
    copy_paste = point.get("qr_code")
    qr_base64 = point.get("qr_code_base64")
    if not payment_id or not copy_paste:
        raise RuntimeError("A API não retornou os dados Pix esperados.")
    await db_execute(
        "INSERT INTO payments(payment_id,user_id,guild_id,amount,status,created_at) VALUES(?,?,?,?,?,?)",
        (payment_id, user_id, guild_id, str(amount), data.get("status", "pending"),
         datetime.now(timezone.utc).isoformat())
    )
    image_file = None
    if qr_base64:
        import base64
        image_file = discord.File(io.BytesIO(base64.b64decode(qr_base64)), filename=f"pix-{payment_id}.png")
    else:
        qr = qrcode.make(copy_paste)
        buffer = io.BytesIO()
        qr.save(buffer, format="PNG")
        buffer.seek(0)
        image_file = discord.File(buffer, filename=f"pix-{payment_id}.png")
    return payment_id, copy_paste, image_file


async def refresh_payment_status(payment_id: str):
    if not MP_ACCESS_TOKEN or http_session is None:
        return None
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with http_session.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}", headers=headers
    ) as response:
        if response.status != 200:
            return None
        data = await response.json(content_type=None)
    status = str(data.get("status", "unknown"))
    await db_execute("UPDATE payments SET status=? WHERE payment_id=?", (status, payment_id))
    return status


async def handle_mp_webhook(request: web.Request):
    # El webhook notifica; el estado siempre se vuelve a consultar a la API del proveedor.
    try:
        body = await request.json()
    except Exception:
        body = {}
    payment_id = str(body.get("data", {}).get("id", "") or request.query.get("data.id", ""))
    topic = str(body.get("type", "") or request.query.get("type", ""))
    if topic not in ("payment", "") or not payment_id.isdigit():
        return web.json_response({"received": True})
    # Validação HMAC-SHA256 do esquema de assinatura do Mercado Pago.
    # x-signature usa ts=...,v1=... e x-request-id.
    if MP_WEBHOOK_SECRET:
        signature_header = request.headers.get("x-signature", "")
        request_id = request.headers.get("x-request-id", "")
        parts = {}
        for part in signature_header.split(","):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                parts[key] = value
        ts = parts.get("ts", "")
        received_v1 = parts.get("v1", "")
        data_id = payment_id.lower()
        if not ts or not received_v1 or not request_id:
            return web.json_response({"error": "missing signature fields"}, status=401)
        manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
        expected = hmac.new(
            MP_WEBHOOK_SECRET.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, received_v1):
            return web.json_response({"error": "invalid signature"}, status=401)
    else:
        log.warning("MP_WEBHOOK_SECRET não configurado; webhook sem validação HMAC.")
    status = await refresh_payment_status(payment_id)
    return web.json_response({"received": True, "status": status})


async def start_webhook():
    global web_runner
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post("/webhook/mercadopago", handle_mp_webhook)
    app.router.add_get("/health", lambda request: web.json_response({"status": "ok"}))
    web_runner = web.AppRunner(app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    log.info("Webhook HTTP escutando na porta %s", WEBHOOK_PORT)


async def pix_for_interaction(interaction: discord.Interaction, value: str):
    try:
        amount = Decimal(value.replace(",", ".")).quantize(Decimal("0.01"))
        if not amount.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await interaction.followup.send("Valor inválido. Exemplo: `/pix valor:48`.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.followup.send("Use este comando dentro do servidor da loja.", ephemeral=True)
        return
    try:
        payment_id, copy_paste, image_file = await create_pix_payment(
            interaction.user.id, interaction.guild.id, amount
        )
    except (RuntimeError, ValueError) as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    embed = make_embed(
        f"**Pedido Pix criado**\n**Valor:** {money(amount)}\n**Pagamento:** `{payment_id}`\n\n"
        "Escaneie o QR Code ou copie o código Pix abaixo. O pedido só será confirmado após validação do provedor.\n\n"
        f"**Pix copia e cola:**\n```{copy_paste[:1800]}```"
    )
    await interaction.followup.send(embed=embed, file=image_file, ephemeral=True)


@bot.event
async def on_ready():
    log.info("Conectado como %s (%s)", bot.user, bot.user.id if bot.user else "?")
    if not getattr(bot, "_persistent_views_added", False):
        bot.add_view(OpenTicketView())
        bot.add_view(CloseTicketView())
        bot._persistent_views_added = True
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("Sincronizados %s comandos no servidor %s", len(synced), GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Sincronizados %s comandos globais", len(synced))
    except Exception:
        log.exception("Falha ao sincronizar comandos slash")
    await load_custom_commands()


async def load_custom_commands():
    if not GUILD_ID:
        return
    rows = await db_fetchall("SELECT name,description,response FROM custom_commands WHERE guild_id=?", (GUILD_ID,))
    guild_obj = discord.Object(id=GUILD_ID)
    for row in rows:
        register_custom_command(GUILD_ID, row["name"], row["description"], row["response"], sync=False)
    try:
        await bot.tree.sync(guild=guild_obj)
    except Exception:
        log.exception("Falha ao sincronizar comandos personalizados")


def register_custom_command(guild_id: int, name: str, description: str, response: str, sync=True):
    safe_name = name.lower().replace(" ", "-")
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", safe_name):
        raise ValueError("Use apenas letras minúsculas, números, hífen e underline (até 32 caracteres).")
    existing = bot.tree.get_command(safe_name, guild=discord.Object(id=guild_id))
    if existing:
        bot.tree.remove_command(safe_name, guild=discord.Object(id=guild_id))

    async def custom_callback(interaction: discord.Interaction):
        safe_response = response.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
        await interaction.response.send_message(safe_response[:2000], allowed_mentions=discord.AllowedMentions.none())

    command = app_commands.Command(
        name=safe_name,
        description=(description or "Comando personalizado")[:100],
        callback=custom_callback
    )
    bot.tree.add_command(command, guild=discord.Object(id=guild_id))
    return safe_name


@bot.tree.command(name="painel", description="Cria e personaliza um embed da loja")
async def painel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Este painel é exclusivo da equipe autorizada.", ephemeral=True)
        return
    await interaction.response.send_modal(EmbedBuilderModal())


@bot.tree.command(name="ticket-painel", description="Publica o painel para abrir tickets")
async def ticket_painel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
        return
    embed = make_embed("Precisa de ajuda com um pedido ou produto? Clique no botão abaixo e preencha o formulário.")
    await interaction.channel.send(embed=embed, view=OpenTicketView())
    await interaction.response.send_message("Painel de tickets publicado.", ephemeral=True)


@bot.tree.command(name="pix", description="Gera uma cobrança Pix pelo Mercado Pago")
@app_commands.describe(valor="Valor em reais, por exemplo 48.00")
async def pix(interaction: discord.Interaction, valor: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await pix_for_interaction(interaction, valor)


@bot.tree.command(name="feedback", description="Avalia sua experiência com a loja")
async def feedback(interaction: discord.Interaction):
    await interaction.response.send_modal(FeedbackModal())


@bot.tree.command(name="comando-criar", description="Cria um comando slash personalizado de resposta")
@app_commands.describe(nome="Nome do comando", descricao="Descrição exibida no Discord", resposta="Texto enviado pelo comando")
async def comando_criar(interaction: discord.Interaction, nome: str, descricao: str, resposta: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.", ephemeral=True)
        return
    try:
        safe_name = register_custom_command(interaction.guild.id, nome, descricao, resposta)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await db_execute(
        "INSERT INTO custom_commands(guild_id,name,description,response) VALUES(?,?,?,?) "
        "ON CONFLICT(guild_id,name) DO UPDATE SET description=excluded.description,response=excluded.response",
        (interaction.guild.id, safe_name, descricao[:100], resposta[:2000])
    )
    try:
        await bot.tree.sync(guild=discord.Object(id=interaction.guild.id))
    except Exception:
        log.exception("Falha ao sincronizar comando personalizado")
    await interaction.response.send_message(f"Comando `/{safe_name}` criado/atualizado.", ephemeral=True)


@bot.tree.command(name="comando-listar", description="Lista comandos personalizados")
async def comando_listar(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.", ephemeral=True)
        return
    rows = await db_fetchall(
        "SELECT name,description FROM custom_commands WHERE guild_id=? ORDER BY name",
        (interaction.guild.id,)
    )
    if not rows:
        text = "Ainda não há comandos personalizados."
    else:
        text = "\n".join(f"`/{r['name']}` — {r['description']}" for r in rows)
    await interaction.response.send_message(text[:1900], ephemeral=True)


@bot.tree.command(name="comando-remover", description="Remove um comando personalizado")
@app_commands.describe(nome="Nome do comando sem a barra")
async def comando_remover(interaction: discord.Interaction, nome: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.", ephemeral=True)
        return
    safe_name = nome.lower().replace(" ", "-")
    await db_execute("DELETE FROM custom_commands WHERE guild_id=? AND name=?", (interaction.guild.id, safe_name))
    bot.tree.remove_command(safe_name, guild=discord.Object(id=interaction.guild.id))
    await bot.tree.sync(guild=discord.Object(id=interaction.guild.id))
    await interaction.response.send_message(f"Comando `/{safe_name}` removido do banco de dados.", ephemeral=True)


@bot.tree.command(name="set-canal-feedback", description="Define o canal para publicar avaliações")
@app_commands.describe(canal="Canal onde os feedbacks serão publicados")
async def set_feedback_channel(interaction: discord.Interaction, canal: discord.TextChannel):
    global FEEDBACK_CHANNEL_ID
    if not is_admin(interaction):
        await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
        return
    FEEDBACK_CHANNEL_ID = canal.id
    await interaction.response.send_message(f"Canal de feedback definido: {canal.mention}. Para persistir após reinício, atualize FEEDBACK_CHANNEL_ID no .env.", ephemeral=True)


@bot.tree.command(name="set-canal-transcripts", description="Define canal de registro de tickets")
@app_commands.describe(canal="Canal privado para registros")
async def set_transcript_channel(interaction: discord.Interaction, canal: discord.TextChannel):
    global TRANSCRIPT_CHANNEL_ID
    if not is_admin(interaction):
        await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
        return
    TRANSCRIPT_CHANNEL_ID = canal.id
    await interaction.response.send_message(f"Canal de registros definido: {canal.mention}. Atualize TRANSCRIPT_CHANNEL_ID no .env para persistir após reinício.", ephemeral=True)


@bot.tree.command(name="saldo", description="Mostra seu saldo de moedas virtuais")
async def saldo(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.", ephemeral=True)
        return
    row = await db_fetchone("SELECT coins FROM wallets WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
    coins = row["coins"] if row else 0
    await interaction.response.send_message(f"💰 Seu saldo: **{coins} moedas**.", ephemeral=True)


@bot.tree.command(name="diario", description="Resgata sua recompensa diária")
async def diario(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.", ephemeral=True)
        return
    today = datetime.now(timezone.utc).date().isoformat()
    row = await db_fetchone("SELECT coins,last_daily FROM wallets WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
    if row and row["last_daily"] == today:
        await interaction.response.send_message("Você já resgatou sua recompensa hoje. Volte amanhã!", ephemeral=True)
        return
    reward = 100
    await db_execute(
        "INSERT INTO wallets(guild_id,user_id,coins,last_daily) VALUES(?,?,?,?) "
        "ON CONFLICT(guild_id,user_id) DO UPDATE SET coins=wallets.coins+excluded.coins,last_daily=excluded.last_daily",
        (interaction.guild.id, interaction.user.id, reward, today)
    )
    await interaction.response.send_message(f"🌸 Você recebeu **{reward} moedas**! Use `/mercado` para ver os itens.", ephemeral=True)


@bot.tree.command(name="mercado", description="Mostra os itens do mercado virtual")
async def mercado(interaction: discord.Interaction):
    embed = make_embed(
        "**Mercado de Inazuma**\n"
        "🧧 `amuleto` — 50 moedas\n"
        "🌸 `flor-sakura` — 100 moedas\n"
        "⚡ `orbe-electro` — 250 moedas\n\n"
        "Use `/comprar item:amuleto` para comprar. Moedas sem valor monetário real."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="comprar", description="Compra um item com moedas virtuais")
@app_commands.choices(item=[
    app_commands.Choice(name="Amuleto — 50 moedas", value="amuleto"),
    app_commands.Choice(name="Flor Sakura — 100 moedas", value="flor-sakura"),
    app_commands.Choice(name="Orbe Electro — 250 moedas", value="orbe-electro"),
])
async def comprar(interaction: discord.Interaction, item: app_commands.Choice[str]):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.", ephemeral=True)
        return
    prices = {"amuleto": 50, "flor-sakura": 100, "orbe-electro": 250}
    price = prices[item.value]
    row = await db_fetchone("SELECT coins FROM wallets WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
    coins = row["coins"] if row else 0
    if coins < price:
        await interaction.response.send_message(f"Saldo insuficiente. Você tem {coins} moedas; precisa de {price}.", ephemeral=True)
        return
    await db_execute("UPDATE wallets SET coins=coins-? WHERE guild_id=? AND user_id=?", (price, interaction.guild.id, interaction.user.id))
    await db_execute(
        "INSERT INTO inventory(guild_id,user_id,item,quantity) VALUES(?,?,?,1) "
        "ON CONFLICT(guild_id,user_id,item) DO UPDATE SET quantity=quantity+1",
        (interaction.guild.id, interaction.user.id, item.value)
    )
    await interaction.response.send_message(f"Compra concluída: **{item.value}** por {price} moedas.", ephemeral=True)


@bot.tree.command(name="inventario", description="Mostra seus itens")
async def inventario(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.", ephemeral=True)
        return
    rows = await db_fetchall("SELECT item,quantity FROM inventory WHERE guild_id=? AND user_id=? AND quantity>0", (interaction.guild.id, interaction.user.id))
    if not rows:
        text = "Seu inventário está vazio. Visite `/mercado`!"
    else:
        text = "\n".join(f"• **{r['item']}** × {r['quantity']}" for r in rows)
    await interaction.response.send_message(text, ephemeral=True)


@bot.command(name="valor")
@commands.guild_only()
async def prefix_pix(ctx: commands.Context, valor: str):
    """Exemplo: !valor 48"""
    try:
        amount = Decimal(valor.replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await ctx.reply("Use `!valor 48` para gerar uma cobrança Pix.")
        return
    if amount < Decimal("1.00") or amount > Decimal("10000.00"):
        await ctx.reply("O valor deve ser entre R$ 1,00 e R$ 10.000,00.")
        return
    try:
        payment_id, copy_paste, image_file = await create_pix_payment(ctx.author.id, ctx.guild.id, amount)
    except (RuntimeError, ValueError) as exc:
        await ctx.reply(str(exc))
        return
    embed = make_embed(
        f"**Cobrança Pix criada**\n**Valor:** {money(amount)}\n**Pedido:** `{payment_id}`\n\n"
        f"**Pix copia e cola:**\n```{copy_paste[:1800]}```"
    )
    await ctx.reply(embed=embed, file=image_file, mention_author=False)


@bot.command(name="ajuda")
async def prefix_help(ctx: commands.Context):
    embed = make_embed(
        "**Comandos Raiden Shogun**\n"
        "`!valor 48` — cria cobrança Pix\n"
        "`/painel` — construtor de embed (equipe)\n"
        "`/ticket-painel` — publica tickets (equipe)\n"
        "`/pix valor:48` — cobrança Pix\n"
        "`/feedback` — avaliar a loja\n"
        "`/saldo`, `/diario`, `/mercado`, `/comprar`, `/inventario`\n"
        "`/comando-criar` — criar comando personalizado (equipe)"
    )
    await ctx.reply(embed=embed, mention_author=False)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    log.exception("Erro em comando de prefixo", exc_info=error)
    try:
        await ctx.reply("Ocorreu um erro ao executar o comando. Confira os logs do bot.")
    except discord.HTTPException:
        pass


async def main():
    global http_session
    await ensure_db()
    timeout = aiohttp.ClientTimeout(total=20)
    http_session = aiohttp.ClientSession(timeout=timeout)
    try:
        await start_webhook()
        async with bot:
            await bot.start(TOKEN)
    finally:
        if web_runner:
            await web_runner.cleanup()
        if http_session:
            await http_session.close()
        if db:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
