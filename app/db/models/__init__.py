# Side-effect imports: registers all ORM mappers so Alembic autogenerate sees them.
from app.db.models.ad_campaign import AdCampaign  # noqa: F401
from app.db.models.agent import Agent  # noqa: F401
from app.db.models.audit_log import AuditLog  # noqa: F401
from app.db.models.conversation import Conversation, Message  # noqa: F401
from app.db.models.knowledge_doc import KnowledgeDoc  # noqa: F401
from app.db.models.llm_call import LLMCall  # noqa: F401
from app.db.models.product import Product  # noqa: F401
from app.db.models.tenant import Tenant  # noqa: F401
from app.db.models.user import User  # noqa: F401
