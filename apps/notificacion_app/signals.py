# notificacion_app/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction
from django.core.cache import cache
import logging

from .models import notificacion
from .utils import enviar_a_grupo

logger = logging.getLogger(__name__)

# ================================
# IMPORTS EXTERNOS (otras apps)
# ================================
try:
    from apps.like_app.models import DetallesLike
except ImportError as e:
    logger.warning(f"No se pudo importar DetallesLike: {e}")
    DetallesLike = None

try:
    from apps.match_app.models import Match  
except ImportError as e:
    logger.warning(f"No se pudo importar Match: {e}")
    Match = None

try:
    from apps.chat_app.models import Mensaje, Chat
except ImportError as e:
    logger.warning(f"No se pudo importar Mensaje/Chat: {e}")
    Mensaje = None
    Chat = None


# --------------------------------------------------------
# Notificación cuando alguien da LIKE
# --------------------------------------------------------
if DetallesLike is not None:
    @receiver(post_save, sender=DetallesLike)
    def crear_notificacion_desde_like(sender, instance, created, **kwargs):
        """
        Crea una notificación cuando un usuario da like a otro.
        """
        if not created:
            return

        logger.info(f"Signal like activado para DetallesLike id={instance.id}")
        
        usuario_emisor = instance.usuarioEmisor  
        usuario_receptor = instance.usuarioReceptor  

        if usuario_receptor is None:
            logger.warning("Usuario receptor no encontrado en DetallesLike")
            return

        # Obtener nombre del emisor
        nombre_emisor = usuario_emisor.nombres if hasattr(usuario_emisor, 'nombres') else usuario_emisor.username
        
        # Solo crear notificación para LIKE, no para DISLIKE
        if instance.estado != 'LIKE':
            logger.info(f"No se crea notificación para DISLIKE")
            return
        
        mensaje = f"❤️ {nombre_emisor} te dio like"
        tipo_notif = notificacion.EVENT_LIKE

        logger.info(f"Creando notificación: {mensaje} para usuario {usuario_receptor.id}")

        try:
            notif = notificacion.objects.create(
                tipo=tipo_notif,
                mensaje=mensaje,
                usuario_destino=usuario_receptor,
                
            )

            logger.info(f"Notificación creada exitosamente: id={notif.id}")

            payload = {
                "id": notif.id,
                "tipo": notif.tipo,
                "mensaje": notif.mensaje,
                "fecha_envio": notif.fecha_envio.isoformat(),
                "usuario_emisor_id": usuario_emisor.id,
                "usuario_emisor_nombre": nombre_emisor,
            }

            # Enviar notificación en tiempo real
            transaction.on_commit(
                lambda: enviar_a_grupo(
                    f"user_{usuario_receptor.id}",
                    "notification_message",
                    payload
                )
            )
            
        except Exception as e:
            logger.error(f"Error al crear notificación de like: {e}")

# --------------------------------------------------------
# Notificación cuando hay MATCH
# --------------------------------------------------------
if Match is not None:
    @receiver(post_save, sender=Match)
    def crear_notificacion_desde_match(sender, instance, created, **kwargs):
        """
        Crea notificaciones para ambos usuarios cuando hay un match.
        Incluye chat_id para que el frontend pueda redirigir al chat.
        """
        if not created:
            return

        logger.info(f"Signal match activado para Match id={instance.id}")
        
        user_a = instance.usuarioA
        user_b = instance.usuarioB

        if not user_a or not user_b:
            logger.warning("Usuarios del match no encontrados")
            return

        # Intentar obtener el chat asociado al match
        chat_id = None
        if Chat is not None:
            try:
                chat = Chat.objects.filter(match=instance).first()
                if chat:
                    chat_id = chat.id
                    logger.info(f"Chat id={chat_id} encontrado para Match id={instance.id}")
            except Exception as e:
                logger.warning(f"No se pudo obtener chat para match: {e}")

        # Para usuario A
        try:
            mensaje_a = f"🎯 ¡Match! Tienes un nuevo match con {user_b.nombres if hasattr(user_b, 'nombres') else user_b.username}"
            notif_a = notificacion.objects.create(
                tipo=notificacion.EVENT_MATCH,
                mensaje=mensaje_a,
                usuario_destino=user_a
            )
            
            payload_a = {
                "id": notif_a.id,
                "tipo": notif_a.tipo,
                "mensaje": notif_a.mensaje,
                "fecha_envio": notif_a.fecha_envio.isoformat(),
                "usuario_match_id": user_b.id,
                "chat_id": chat_id,  # Incluir chat_id para navegación
            }
            
            transaction.on_commit(
                lambda: enviar_a_grupo(
                    f"user_{user_a.id}", 
                    "notification_message", 
                    payload_a
                )
            )
            logger.info(f"Notificación de match creada para usuario A: {user_a.id}")
            
        except Exception as e:
            logger.error(f"Error al crear notificación de match para usuario A: {e}")

        # Para usuario B
        try:
            mensaje_b = f"🎯 ¡Match! Tienes un nuevo match con {user_a.nombres if hasattr(user_a, 'nombres') else user_a.username}"
            notif_b = notificacion.objects.create(
                tipo=notificacion.EVENT_MATCH,
                mensaje=mensaje_b,
                usuario_destino=user_b
            )
            
            payload_b = {
                "id": notif_b.id,
                "tipo": notif_b.tipo,
                "mensaje": notif_b.mensaje,
                "fecha_envio": notif_b.fecha_envio.isoformat(),
                "usuario_match_id": user_a.id,
                "chat_id": chat_id,  # Incluir chat_id para navegación
            }
            
            transaction.on_commit(
                lambda: enviar_a_grupo(
                    f"user_{user_b.id}", 
                    "notification_message", 
                    payload_b
                )
            )
            logger.info(f"Notificación de match creada para usuario B: {user_b.id}")
            
        except Exception as e:
            logger.error(f"Error al crear notificación de match para usuario B: {e}")

# --------------------------------------------------------
# Notificación cuando llega un MENSAJE de chat
# --------------------------------------------------------
if Mensaje is not None:
    @receiver(post_save, sender=Mensaje, dispatch_uid='notificar_mensaje_chat')
    def notificar_mensaje_chat(sender, instance, created, **kwargs):
        if not created:
            return

        mensaje = instance
        chat = mensaje.chat
        remitente = mensaje.remitente

        usuarioA = chat.match.usuarioA
        usuarioB = chat.match.usuarioB

        # Identificar receptor
        receptor = usuarioB if remitente == usuarioA else usuarioA

        if receptor is None:
            return

        # =====================================
        # No enviar notificación si el receptor
        # está dentro del chat en ese momento
        # =====================================
        cache_key = f"chat_abierto_usuario_{receptor.id}"
        chat_abierto_id = cache.get(cache_key)

        if chat_abierto_id == chat.id:
            return  # No enviar

        # =====================================
        # Buscar notificación existente para este chat
        # Si existe, actualizarla en vez de crear una nueva
        # =====================================
        from django.utils import timezone
        
        texto = f"{remitente.nombres} te envió un mensaje"
        
        # Buscar notificación existente para este chat y receptor
        notif_existente = notificacion.objects.filter(
            tipo=notificacion.EVENT_CHAT,
            usuario_destino=receptor,
            chat_relacionado=chat
        ).first()
        
        if notif_existente:
            # Actualizar la notificación existente
            notif_existente.mensaje = texto
            notif_existente.fecha_envio = timezone.now()
            notif_existente.estado = notificacion.STATUS_PENDING  # Marcar como no leída de nuevo
            notif_existente.save()
            notif = notif_existente
            logger.info(f"Notificación de chat actualizada: {notif.id}")
        else:
            # Crear nueva notificación
            notif = notificacion.objects.create(
                tipo=notificacion.EVENT_CHAT,
                mensaje=texto,
                usuario_destino=receptor,
                chat_relacionado=chat,  # Guardar referencia al chat
            )
            logger.info(f"Nueva notificación de chat creada: {notif.id}")

        payload = {
            "id": notif.id,
            "tipo": notif.tipo,
            "mensaje": notif.mensaje,
            "fecha_envio": notif.fecha_envio.isoformat(),
            "chat_id": chat.id,  # Incluir el ID del chat para navegación
        }

        transaction.on_commit(
            lambda: enviar_a_grupo(
                f"user_{receptor.id}",
                "notification_message",
                payload
            )
        )