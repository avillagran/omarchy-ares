# Omarchy Ares

## 1. Propósito y estado del plan

Ares es la reescritura del plugin actual con una organización centrada en **proyectos → salas de chat**. Conserva la identidad visual de los bots, la información constante sobre su trabajo, las cuotas y el acceso desde otros dispositivos.

**Idea principal:** cada trabajo tiene un lugar; todo lo que ocurre durante ese trabajo se puede seguir en vivo y volver a leer en su sala.

Este documento es un plan en construcción. Distingue requisitos confirmados, propuestas y decisiones pendientes. Las referencias apuntan a archivos del proyecto actual: son material para reutilizar o aprender, no una declaración de que Ares ya esté implementado. La revisión corresponde al árbol de trabajo actual, que incluye cambios en curso.

En esta etapa se acuerdan necesidades y experiencia de uso. Los detalles internos de gestión de sesiones se resolverán durante el diseño técnico con comportamientos predeterminados razonables; no requieren que el usuario defina ahora cada transición o caso particular.

**Decisión de ejecución confirmada:** antes de implementar, hacer un **respaldo completo y verificado de todo lo actual**. Ares se construirá en un **proyecto limpio y separado**, incorporando progresivamente solo lo que requiera este plan. El proyecto actual queda preservado como referencia y punto de recuperación.

## 2. Requisitos confirmados

### Lo que queremos conservar

- **Widget con cara animada:** muestra cuándo hay bots trabajando.
- **Información constante:** actividad y notificaciones informativas durante el trabajo.
- **Cuotas y consumo tal como están:** conservar presentación y significado; agregar pequeñas líneas verticales que dividan la barra por días según la duración del plan o ventana.
- **Gestión de bots:** identidad, caras, modelo y demás configuración.
- **Conexión automática a peers.**
- **Acceso web desde el teléfono.**

### La organización nueva

- Un proyecto representa una carpeta de código fuente o un repositorio y puede contener varias salas de chat.
- Poder agrupar proyectos que funcionan de manera similar; la forma concreta de agrupación queda por definir.
- Las salas se pueden pausar para reducir el consumo de memoria de sus sesiones de Hermes, tanto locales como en los nodos conectados, y eliminar para organizar el trabajo.
- **No existen chats sueltos:** toda conversación pertenece a una sala y toda sala pertenece a un proyecto.
- **Toda la actividad del trabajo se puede leer dentro de la sala:** mensajes, llamadas a herramientas, resultados, estados, errores e intervenciones.
- **La información aparece en tiempo real:** texto incremental, carácter por carácter, y actividad de herramientas desde que comienza, sin esperar solamente la respuesta final.
- **Imágenes y videos visibles en ambas direcciones:** poder ver dentro de la sala lo que el usuario envía al bot y lo que el bot devuelve, tanto en escritorio como desde el teléfono. Es una capacidad central de la conversación.
- **Rediseño visual integral:** encajar con la UI y el tema de Omarchy, con una experiencia más cuidada, animada y fresca. Conservar las funciones e identidad de los bots no implica conservar la composición gráfica actual.

## 3. Modelo del producto

### Proyecto

**Definición confirmada:** un proyecto representa una carpeta de código fuente o un repositorio. Tiene identidad estable, nombre, ubicación de trabajo y sus salas. Crear una sala requiere un proyecto; no se permite una sala sin asignación ni siquiera si se crea desde el widget o el teléfono.

La ubicación debe identificar el nodo y la carpeta: una misma ruta en dos máquinas no garantiza que sea el mismo proyecto. Queda por definir cómo se relacionan distintas copias o worktrees de un repositorio y qué ubicación utiliza cada bot remoto. Agrupar proyectos no implica copiar ni sincronizar automáticamente su código.

### Agrupación de proyectos

**Dirección acordada:** permitir reunir proyectos que funcionan de forma similar. **Propuesta de organización:** una capa opcional «Grupo de proyectos» por encima de los proyectos, manteniendo la estructura **grupo → proyectos → salas**. Un proyecto también puede existir sin grupo; una sala siempre pertenece a un proyecto concreto.

Ejemplo: el grupo «Plugins Omarchy» reúne varios repositorios de plugins, cada uno con sus propias salas. La agrupación facilita navegar y encontrar trabajo relacionado sin fusionar historiales ni directorios.

Queda por decidir si el grupo solo organiza o también permite compartir instrucciones, convenciones y configuración. Las salas que abarcan varios repositorios requieren una decisión posterior; no se asume que un grupo tenga chats independientes de los proyectos.

### Sala de chat

Es la unidad de conversación, seguimiento y control del trabajo. Contiene participantes, historial, actividad en vivo, borrador y estado de organización.

**Propuesta:** una sala puede tener un bot o varios. Hablar con un solo bot usa la misma experiencia que una sala colaborativa; no genera una categoría paralela de chats individuales.

Una sala puede necesitar varias sesiones técnicas de Hermes. Esas sesiones quedan asociadas a la sala: su actividad debe aparecer allí, aunque la interfaz no obligue al usuario a administrar identificadores internos.

### Bot

Es una identidad reutilizable: nombre, cara, color, modelo, proveedor y ubicación local o remota. Puede participar en varias salas sin mezclar sus conversaciones.

**Propuesta:** conservar un catálogo de bots y seleccionarlos al crear o configurar una sala. La vista de bots administra identidades; la acción «conversar» conduce a elegir proyecto/sala o crear una sala dentro de un proyecto.

### Ejecución y evento

Una ejecución es una unidad de trabajo de un bot dentro de una sala. Sus eventos permiten reconstruir qué ocurrió y en qué orden: envío aceptado, entrada en cola, inicio, texto recibido, herramienta iniciada, resultado, espera, finalización o error.

El estado de la sala y el de una ejecución son distintos: «sala activa» no significa «bot trabajando», y perder conexión no significa que el trabajo terminó.

### Reglas de pertenencia

- Todo envío tiene proyecto y sala antes de iniciar una ejecución.
- Todo mensaje, herramienta e intervención del trabajo se vincula a su sala, ejecución y autor cuando corresponda.
- Las notificaciones sobre trabajo llevan al proyecto y sala de origen.
- Un enlace, una búsqueda o una vista global puede mostrar una sala fuera de su listado, pero no elimina su pertenencia al proyecto.
- Las sesiones creadas desde otras interfaces necesitan una regla explícita de incorporación; no deben reaparecer como chats sueltos en Ares.

## 4. Experiencia de uso propuesta

### Dirección visual: parte de Omarchy, con personalidad propia

**Requisito confirmado:** la apariencia actual del plugin no es la referencia estética a conservar. Ares necesita un rediseño que siga el lenguaje visual de Omarchy y eleve la calidad de la experiencia: coherente, legible, fluido y con vida.

La personalidad de Ares se apoya en las caras de sus bots, su actividad y las transiciones de la interfaz. El objetivo es que se sienta una pieza cuidada del escritorio desde que se abre el widget hasta que se revisa una conversación con imágenes y videos.

### Integración con el estilo y el tema

- Usar el tema activo de Omarchy como base de colores, tipografía, superficies, bordes, espaciado y controles. Tomar como referencia sus componentes reales durante el diseño, sin fijar ahora una paleta independiente.
- Responder a los cambios de tema manteniendo legibilidad, contraste y significado de los estados. La identidad de cada bot sigue siendo reconocible con distintos fondos y acentos.
- Compartir criterios visuales entre widget, panel, conversación, diálogos y web. Propuesta: que la web reciba la identidad visual del Omarchy que sirve Ares, también al abrirla desde el teléfono.
- Definir una pequeña biblioteca visual común: botones, campos, menús, listas, tarjetas de herramientas, avisos, adjuntos y estados. Evitar que cada vista tenga tamaños, iconos o comportamientos distintos.
- Mantener las cuotas y consumo con su presentación y significado ya acordados, sumando los separadores diarios. El rediseño del entorno debe respetar esa parte que el usuario quiere conservar.

### Composición propuesta

- **Proyectos y salas fáciles de recorrer:** jerarquía clara, selección reconocible y actividad visible junto a cada sala.
- **Conversación como protagonista:** espacio para leer texto, código, imágenes y videos; encabezado compacto con proyecto, sala y participantes.
- **Herramientas legibles:** resumen de actividad y detalles desplegables dentro del historial, con una presentación consistente y sin perder información.
- **Compositor cuidado:** texto, adjuntos con vista previa y acción de envío agrupados; estados de carga y errores junto a lo que afectan.
- **Gestión accesible:** bots, peers y configuración disponibles sin competir permanentemente con la conversación.
- **Densidad adaptable:** aprovechar el escritorio y reorganizarse en el teléfono, conservando jerarquía y controles cómodos en lugar de encoger toda la interfaz.

Esta es una dirección para desarrollar bocetos, no un diseño de pantallas cerrado ni una elección del motor gráfico.

### Movimiento e interacción

- Conservar y pulir las caras animadas como firma visual de Ares: actividad, descanso y atención reconocibles.
- Animar apertura/cierre, selección de salas, despliegue de detalles y aparición de avisos con transiciones breves y coherentes.
- Dar respuesta inmediata a hover, foco, pulsación, envío y carga de adjuntos. Que las acciones se sientan claras y ágiles.
- Presentar la llegada de mensajes, herramientas y resultados multimedia con continuidad visual, sin saltos de scroll ni cambios bruscos de tamaño que dificulten leer.
- Hacer que las animaciones de actividad reflejen estados reales. La animación de presentación no debe retrasar el texto recibido ni prolongar artificialmente un trabajo terminado.
- Respetar movimiento reducido y navegación por teclado. El estado debe entenderse también mediante texto e iconos.
- Detener animaciones fuera de vista o innecesarias en reposo. Una interfaz fresca y fluida debe acompañar el objetivo de gestionar bien memoria y recursos.

### Referencias existentes y validación visual

[BarWidget.qml](BarWidget.qml), [Panel.qml](Panel.qml) y [RoomAnimatedScene.qml](RoomAnimatedScene.qml) ya usan componentes de `qs.Ui` y valores de `Style` y `Color` de `qs.Commons`. Sirven para identificar puntos de integración con Omarchy, no como maqueta del nuevo diseño. [BotFace.qml](BotFace.qml) y [BotAvatar.qml](BotAvatar.qml) aportan la identidad que sí queremos conservar y mejorar. [web/v2/style.css](web/v2/style.css) contiene una paleta y tipografía propias fijadas en CSS: necesita una estrategia de tema compartido para cumplir esta dirección.

**Trabajo de diseño previsto:** revisar las superficies reales de Omarchy y preparar bocetos de widget, navegación de proyectos/salas, conversación activa, herramientas y multimedia, junto con su adaptación móvil. Definir su comportamiento animado antes de dar por cerrada la elección del cliente. En esta etapa queda documentada la dirección; aún no se implementa la UI.

**Criterios de aceptación:** Ares se reconoce como parte de Omarchy con distintos temas; los controles y la jerarquía son consistentes; las caras y transiciones aportan vida; texto y multimedia se leen cómodamente en escritorio y teléfono; recibir actividad no interrumpe la lectura. Evaluar tanto capturas como recorridos en movimiento: una captura estática no demuestra fluidez.

### Navegación

1. Abrir Ares desde el widget.
2. Elegir un proyecto, directamente o desde su grupo, y ver sus salas activas y pausadas.
3. Abrir una sala: participantes, historial completo y actividad en vivo en el mismo lugar.
4. Escribir, seguir el trabajo e intervenir cuando se solicite.
5. Pausar, reanudar o eliminar la sala según su ciclo de trabajo.

Bots, peers y cuotas siguen siendo accesibles como gestión y supervisión. La navegación principal se organiza alrededor de proyectos y salas.

### Widget y caras

- Conservar las caras con movimiento de ojos y estado de descanso.
- Mostrar quién trabaja y una descripción breve de su actividad; cuando hay varios bots, conservar la rotación de actividad.
- Mantener indicaciones de atención: una aprobación o pregunta pendiente debe ser fácil de detectar.
- Abrir la sala correspondiente al seleccionar una actividad o aviso.
- Animar «trabajando» a partir de señales confirmadas. Mostrar desconexión o señal desactualizada cuando corresponda.

**Referencias:** [BarWidget.qml](BarWidget.qml), [BotFace.qml](BotFace.qml), [BotAvatar.qml](BotAvatar.qml), [BotStatusStore.qml](BotStatusStore.qml). Ya existen cara de trabajo, ticker, contador de atención e identidad visual compartida.

### Gestión de sesiones: pausar y retomar trabajo

**Necesidad confirmada:** al abrir salas para distintas tareas o funcionalidades, Ares debe gestionar las sesiones de Hermes que va creando. Tener muchas conversaciones guardadas no debería implicar mantener todas sus sesiones cargadas en memoria, ni localmente ni en los nodos conectados.

Pausar expresa una intención sencilla: **«por ahora no trabajo en esto; conserva lo necesario para retomarlo y libera los recursos que ya no necesita»**. El usuario organiza sus salas; Ares se encarga del ciclo de vida de las sesiones asociadas.

Comportamiento esperado:

- Saber qué sesiones pertenecen a cada sala y en qué nodo se ejecutan, evitando acumulaciones sin gestionar y duplicaciones innecesarias.
- Poder dejar una sala en pausa y retomarla después conservando historial y contexto persistido.
- Reducir los recursos de las sesiones que dejan de usarse mediante las capacidades reales de Hermes.
- Consultar el historial de una sala pausada sin tener que activar sus bots, también desde el teléfono.
- Respetar las pausas al reiniciar o reconectar y mantener disponibles las demás salas.
- Informar qué está ocurriendo y cualquier problema que impida pausar o retomar, sin exigir al usuario administrar procesos o sesiones internas.

**A resolver durante el diseño técnico:** cómo guardar y descargar las sesiones, cómo tratar el trabajo en curso y cómo coordinar los nodos. Se elegirán comportamientos predeterminados que preserven el trabajo y se comprobará el ahorro real de recursos. Estas decisiones no bloquean la definición del producto ni requieren una elección del usuario ahora. Una política automática para sesiones inactivas puede evaluarse más adelante como complemento de la pausa manual.

**Referencias para esa investigación:** [server/src/main.rs](server/src/main.rs) ya usa `session.resume`; [docs/bot-server-architecture.md](docs/bot-server-architecture.md) describe sesiones y ejecución distribuida. Falta verificar las capacidades concretas de descarga: detener un turno o desconectar una interfaz no demuestra por sí solo liberación de memoria.

**Criterio de aceptación:** poder acumular salas de distintas tareas sin mantener todas sus sesiones residentes; comprobar reducción de recursos al pausarlas y recuperación del trabajo al retomarlas, en local y en un nodo remoto. Leer el historial no debe recargar las sesiones ni afectar a otras salas. Un nodo inaccesible debe mostrarse como pendiente de confirmar, no como memoria ya liberada.

### Eliminar salas

**Requisito confirmado:** poder eliminarlas. Queda por definir si eliminar significa retirar de la organización conservando una recuperación temporal o borrar definitivamente su historial.

En cualquier variante, debe resolverse el trabajo en curso antes de declarar completada la eliminación; una sala eliminada no puede dejar ejecuciones sin un lugar donde consultar su resultado. La operación se sincroniza entre clientes y no elimina las identidades de los bots participantes.

## 5. La sala como registro completo del trabajo

### Qué debe poder leerse

- Mensajes del usuario y respuestas de cada bot, con atribución clara.
- Imágenes y videos enviados o recibidos, con visualización y reproducción dentro de la conversación.
- Texto parcial mientras se genera y su versión final reconciliada, sin duplicarla.
- Llamadas a herramientas: nombre, parámetros disponibles, inicio, progreso disponible, salida, resultado o error.
- Delegaciones entre bots, participantes seleccionados y decisiones de ejecución observables, incluso cuando un bot termina sin respuesta pública.
- Solicitudes de aprobación o aclaración y su resolución.
- Entradas en cola, pausas, cancelaciones, reintentos, interrupciones y fallos.
- Cambios de participantes o configuración relevantes para el trabajo.
- Pérdida y recuperación de conexión cuando afectan a esa sala.
- Razonamiento o pensamiento compartido cuando el proveedor realmente lo exponga.

**Regla de visibilidad:** un resumen, una burbuja animada, un aviso del sistema o un feed global nunca es el único lugar donde aparece un evento del trabajo. La sala conserva su registro consultable. Los detalles extensos pueden desplegarse, pero no desaparecer al finalizar el turno.

Los valores de credenciales no forman parte del historial: se registra la solicitud y su resolución. Si la fuente no entrega cierto detalle, Ares lo indica en lugar de inventarlo. «Todo visible» requiere capturar lo observable y hacer explícitos los límites de la fuente.

**Pendiente:** decidir dónde se leen los eventos globales sin una sala natural, como descubrir un peer o cambiar una cuota. Los eventos que afectan a una sala se referencian allí. Para los demás falta acordar una ubicación compatible con el modelo: la propuesta anterior de un «proyecto de administración» genérico no encaja con la definición confirmada de proyecto como código fuente.

### Historial y actividad viva son una continuidad

La vista en vivo y el historial deben representar los mismos hechos. Una herramienta que se vio ejecutar debe seguir siendo consultable al cerrar y volver a abrir la sala, también desde el teléfono.

Esto necesita distinguir el registro duradero de actividad del buffer de reconexión: un stream con retención limitada no alcanza por sí solo para conservar el historial completo. Si un resultado grande se guarda aparte, la sala mantiene su referencia y permite abrirlo. La política de conservación se definirá junto con la eliminación de salas.

**Referencias actuales:** [RoomActivity.js](RoomActivity.js), [RoomActivityBubble.qml](RoomActivityBubble.qml), [RoomAnimatedScene.qml](RoomAnimatedScene.qml), [EventFeed.qml](EventFeed.qml) y [docs/bot-server-architecture.md](docs/bot-server-architecture.md). Hay presentación de actividad real y eventos recuperables; falta validar una cobertura duradera y completa para el contrato de Ares.

### Imágenes y videos como parte de la conversación

**Necesidad confirmada:** poder ver lo que se intercambia con el bot. Una imagen o un video debe aparecer como contenido utilizable dentro de la sala, con su mensaje y autor, en ambos sentidos: **usuario → bot** y **bot → usuario**.

#### Lo que envía el usuario

- Adjuntar imágenes y videos desde el dispositivo. Propuesta de interacción: selector de archivos, arrastrar y soltar, y pegar imágenes desde el portapapeles donde esté disponible.
- Ver una vista previa antes de enviar, reproducir el video para comprobar su contenido y poder quitar un adjunto del borrador.
- Mostrar el progreso de carga, la confirmación de envío y cualquier error; conservar el borrador y sus adjuntos ante fallos.
- Después del envío, mantener visibles los archivos junto al texto del mensaje, para saber exactamente qué se compartió.

#### Lo que devuelve el bot

- Mostrar las imágenes incluidas en una respuesta o producidas por sus herramientas: capturas de pantalla, gráficos, diseños y otros resultados visuales.
- Reproducir los videos que el bot entregue, incluidos los generados o recuperados mediante herramientas.
- Incorporar los resultados multimedia a la sala cuando estén disponibles, sin exigir copiar una ruta a otra aplicación para poder verlos.
- Conservar la relación con el mensaje o la herramienta que produjo cada archivo, evitando que quede perdido entre los eventos de ejecución.

#### Experiencia de visualización

- **Imágenes:** vista integrada y apertura ampliada con zoom, además de guardar/descargar el original.
- **Videos:** reproductor dentro de la conversación, con reproducir/pausar, avance por la línea de tiempo, volumen cuando corresponda y pantalla completa.
- **Historial:** los contenidos siguen disponibles al reabrir la sala y al pausarla, sin activar las sesiones de los bots.
- **Escritorio y teléfono:** acceder al mismo contenido desde ambos; una ruta local en el equipo de un bot no basta como entrega al usuario.
- **Recursos:** cargar vistas previas y contenido bajo demanda; recorrer muchas salas no debe mantener todos sus videos y originales cargados en memoria.

### Entrega real al modelo y conservación de archivos

Ver un archivo en la interfaz y lograr que el modelo lo procese son dos partes del mismo recorrido que deben verificarse por separado. Ares debe entregar el contenido mediante las capacidades del modelo y del transporte elegidos. Si una ruta no admite imágenes o video, debe indicarlo claramente y conservar el adjunto; no dar por enviado al modelo un archivo del que solo recibió el nombre o una ruta inaccesible. Cualquier adaptación, como extraer fotogramas de un video, debe quedar explícita y se evaluará durante el diseño técnico.

Los archivos necesitan una identidad y una ubicación gestionadas, asociadas a la sala y accesibles desde los clientes y nodos que los necesiten. Los resultados generados en un peer deben poder llegar al usuario; los adjuntos enviados desde el teléfono deben llegar al nodo que ejecuta el bot. Su conservación no debe depender de que siga vivo un proceso, exista una URL temporal o permanezca abierta la pestaña que los subió. La política de eliminación se definirá junto con la del historial.

**Base actual y brecha a resolver:** [Panel.qml](Panel.qml) ya contiene selección, pegado y algunas vistas de imágenes; [web/app.mjs](web/app.mjs) incluye vistas previas de imágenes y adjuntos en el historial mediante `renderAttachments` y `attachHistory`. [web/core.mjs](web/core.mjs) tiene operaciones de carga/descarga y envío de adjuntos para salas. [docs/mobile-api.md](docs/mobile-api.md) documenta almacenamiento y límites actuales. Son piezas parciales: hay rutas de conversación y peers con restricciones de adjuntos, y las vistas de imágenes no resuelven por sí solas reproducción de video ni el recorrido completo en ambos sentidos. Los límites actuales deben revisarse para los tamaños habituales de video.

**Criterios de aceptación:** enviar una imagen y un video, comprobar su vista previa y encontrarlos en el historial; recibir una imagen y un video de un bot/herramienta y verlos dentro de la sala; repetir el recorrido desde el teléfono y con un bot en un nodo remoto. Verificar entrega efectiva en una ruta/modelo compatible, errores explícitos en una incompatible y acceso a los archivos después de pausar la sala o reiniciar el servicio. Estos escenarios deben orientar la elección del cliente de escritorio desde el inicio.

## 6. Tiempo real de extremo a extremo

### Contrato esperado

- Publicar cada fragmento de texto en cuanto lo entregue Hermes/proveedor y renderizarlo incrementalmente.
- Mostrar inicio y final de una herramienta como eventos propios; incorporar su progreso y salida incremental si la herramienta los entrega.
- Identificar eventos, mensajes y llamadas de herramientas de manera estable para ordenar, actualizar y evitar duplicados.
- Retomar desde el último evento confirmado al reconectar. Si falta un tramo, recuperar historial y declarar cualquier hueco irrecuperable.
- Conservar el borrador hasta la confirmación de aceptación del envío; aceptar un envío no significa haber terminado el trabajo.
- Mantener lectura y posición de scroll: si el usuario revisa mensajes anteriores, indicar nueva actividad sin arrastrarlo al final.
- Mantener estados coherentes entre widget, sala y web, incluso cuando el panel esté cerrado.

### Carácter por carácter y entrega real

El requisito visual es mostrar la respuesta carácter por carácter. La fuente puede entregar tokens o fragmentos con varios caracteres; Ares debe consumirlos inmediatamente y resolver la presentación sin acumular un retraso artificial que haga parecer activo un trabajo ya terminado.

Hoy [RoomActivity.js](RoomActivity.js) contiene `reconcileStream` y `revealNext`, que reconcilian texto acumulado y lo revelan progresivamente. Es una referencia útil para la presentación y el manejo de Unicode. No demuestra por sí sola streaming extremo a extremo: también se usa sondeo y lectura de instantáneas.

[BotServerBridge.qml](BotServerBridge.qml) y [server/src/main.rs](server/src/main.rs) aportan cursores, eventos y reconexión. [web/v2/README.md](web/v2/README.md) documenta además límites de la interfaz experimental y su refresco por instantáneas. Ares debe verificar el recorrido completo desde la fuente hasta ambos clientes, no asumirlo por contar con SSE o WebSocket.

**Aceptación:** ver una respuesta parcial y una herramienta iniciada antes de que termine el turno, tanto en escritorio como en teléfono; cortar la conexión y recuperar la continuidad sin repetir ejecuciones ni perder el historial disponible. Queda por acordar un objetivo medible de latencia.

## 7. Notificaciones e información constante

- Conservar el ticker de actividad, los avisos informativos y los indicadores de atención.
- Informar inicios, esperas, aprobaciones, errores, finalizaciones y cambios de conexión relevantes.
- Registrar en la sala el hecho que originó cada aviso del trabajo.
- Al pulsar un aviso, abrir su sala y, cuando sea posible, el evento correspondiente.
- Evitar avisos duplicados al reconectar o reproducir eventos antiguos.

**Propuesta de presentación:** el texto y las herramientas se actualizan continuamente dentro de la sala y el widget; las notificaciones emergentes acompañan los cambios significativos. Su frecuencia se ajustará conservando la sensación de información constante que tiene el plugin actual.

**Referencias:** [EventFeed.qml](EventFeed.qml), las funciones `notifyApproval` y `notifyLinkEvent` de [Panel.qml](Panel.qml), [bin/hermes-notify-attention](bin/hermes-notify-attention) y [bin/hermes-notify-link](bin/hermes-notify-link).

## 8. Cuotas y consumo: conservar y dividir por días

### Mantener tal como está

- Tarjetas por proveedor y plan, ventanas de cuota y porcentaje disponible.
- Colores y barra de disponibilidad, fecha/tiempo de reinicio y actualización manual.
- Consumo observado de tokens y llamadas en los períodos actuales.
- Distinción actual entre actividad local y cuota real del proveedor.
- Valores en caché con su antigüedad cuando no se logra actualizar.
- Resumen compacto y detalle desplegable.

### Único cambio visual solicitado

Agregar pequeñas líneas verticales sobre la barra existente para dividirla en días equivalentes de la ventana del plan.

- Ventana semanal: **7 segmentos y 6 separadores interiores**.
- Ventana de 30 días: 30 segmentos, si esa es la duración real conocida.
- Mantener el relleno como porcentaje **disponible**, con su orientación actual.
- Las divisiones son una referencia de reparto diario de la cuota; no representan consumo medido de cada día ni un reinicio diario.
- Usar la duración real de la ventana, no deducirla solamente del nombre comercial del plan. Para períodos de calendario, usar sus fechas reales.
- Ventanas menores a un día o de duración desconocida conservan su barra sin divisiones diarias inventadas.

**Pendiente de diseño:** confirmar si basta con los separadores o si conviene una pequeña referencia del día actual/restante para comparar disponibilidad con el avance del período. Esa referencia sería adicional al cambio solicitado.

**Referencias concretas:** sección «Cuotas y consumo» de [Panel.qml](Panel.qml); `_quota_window`, `_parse_openai_quota`, `_parse_kimi_quota` y `provider_usage` en [bin/hermes-bots](bin/hermes-bots). Ya se exponen `windowSeconds`, `resetAt` y `availablePercent` para varias ventanas. [tests/test_quota_stale_marker.py](tests/test_quota_stale_marker.py) protege el comportamiento de caché.

Actualmente las cuotas dependen de la interfaz/helper y no tienen endpoint en el servidor, según [docs/v3-contract-inventory.md](docs/v3-contract-inventory.md). Propuesta para Ares: compartir esos mismos datos mediante el servicio común para que el teléfono pueda mostrar la misma información, preservando su semántica y diseño.

## 9. Gestión de bots y peers

### Bots

Conservar creación y edición de bots, nombre visible, descripción, cara/forma/color, proveedor y modelo. Mostrar la identidad de manera consistente en catálogo, participantes, mensajes y actividad.

**Pendiente:** decidir si el modelo se configura solamente en el bot o admite una selección por sala. Si cambia durante el trabajo, el historial debe permitir saber qué configuración ejecutó cada turno.

**Referencias:** [BotAvatar.qml](BotAvatar.qml), [BotFace.qml](BotFace.qml), controles de bots en [Panel.qml](Panel.qml), operaciones `--create-bot` y `--update-bot` de [bin/hermes-bots](bin/hermes-bots), y rutas de bots en [server/src/main.rs](server/src/main.rs).

### Peers automáticos

- Descubrir y reconectar peers en segundo plano, sin tener que abrir su vista.
- Conservar los peers conocidos entre sesiones y actualizar su disponibilidad.
- Hacer accesibles sus bots desde las salas con atribución de host e identidad estable.
- Informar conexión, pérdida de señal y recuperación; conservar la lectura del historial al desconectarse.

**Base existente:** `peerAutoDiscoveryTimer` en [Panel.qml](Panel.qml) se ejecuta al inicio y cada 60 segundos; `discover_peers` y `sync_peers` de [bin/hermes-bots](bin/hermes-bots) descubren gateways LAN y registran candidatos cuando autentican con la clave compartida configurada. Ares debe conservar esa automatización y definir el comportamiento para peers ya configurados fuera de la LAN y cambios de dirección.

## 10. Acceso web desde el teléfono

La web ofrece los mismos proyectos, salas, participantes y registro de actividad. Abrir una sala en el teléfono no crea otra conversación ni una copia independiente de su estado.

- Conservar acceso por enlace/QR y controles de conexión.
- Navegar proyectos y salas; enviar mensajes y ver texto/herramientas en vivo.
- Enviar, visualizar y ampliar imágenes; enviar y reproducir videos dentro de las salas, con acceso al mismo historial multimedia que en escritorio.
- Pausar, reanudar y eliminar con la misma semántica del escritorio.
- Recuperar conexión e historial al volver a la página.
- Conservar borradores y distinguir aceptación del mensaje de finalización del trabajo.
- Mantener enlaces directos a las salas y paridad de estado entre dispositivos.

**Referencias:** [MobileAccessDialog.qml](MobileAccessDialog.qml), [bin/hermes-web](bin/hermes-web), [bin/hermes_web.py](bin/hermes_web.py), [web/core.mjs](web/core.mjs), [web/storage.mjs](web/storage.mjs), [docs/mobile-api.md](docs/mobile-api.md) y [web/v2/](web/v2/).

Hay caminos actuales y experimentales con capacidades diferentes; la existencia de la interfaz v2 no equivale a paridad completa. La decisión del cliente de escritorio queda abierta. [README.md](README.md#renderer-compatibility) registra un problema concreto al incrustar WebEngine en Quickshell que debe considerarse si se evalúa esa alternativa.

## 11. Base técnica propuesta para la reescritura

### Responsabilidades

- **Hermes:** ejecución de los bots, modelos, herramientas y sesiones mediante sus interfaces disponibles.
- **Servicio Ares:** agrupaciones y proyectos, pertenencia de salas, ciclo de vida, asociación de sesiones con nodos, coordinación de descarga/reanudación, registro de actividad, archivos multimedia y su entrega entre nodos/clientes, sincronización y conexiones.
- **Clientes de escritorio y web:** presentación, visualización de imágenes y reproducción de video, interacción y borradores; consumen el mismo estado, archivos y eventos.

Propuesta: aprovechar el servidor Rust actual como referencia y seleccionar las piezas que sirvan al servicio común. Su incorporación se hará dentro del proyecto limpio, verificando sus contratos y adaptándolas a Ares; la estructura inicial se define para el nuevo producto.

### Lo que ya sirve como referencia

| Área | Archivos actuales | Qué aprovechar / qué revisar |
| --- | --- | --- |
| Identidad visual y presencia | [BotFace.qml](BotFace.qml), [BotAvatar.qml](BotAvatar.qml), [BotStatusStore.qml](BotStatusStore.qml) | Caras compartidas y actividad confirmada por identidad. |
| Widget y atención | [BarWidget.qml](BarWidget.qml), [EventFeed.qml](EventFeed.qml) | Ticker, avisos, acceso rápido; asociarlos siempre a salas. |
| Estado común y transporte | [server/src/main.rs](server/src/main.rs), [BotServerBridge.qml](BotServerBridge.qml) | Eventos, cursores, revisiones, recibos de envío y reconexión. |
| Integración con Hermes | [bin/hermes_desktop_rpc.py](bin/hermes_desktop_rpc.py), [bin/hermes-bots](bin/hermes-bots) | Capacidades reales, adaptación y descubrimiento; delimitar dependencias. |
| Actividad y lectura | [RoomActivity.js](RoomActivity.js), [web/v2/reconcile.mjs](web/v2/reconcile.mjs), [web/v2/viewport.mjs](web/v2/viewport.mjs) | Texto incremental, identidades estables y scroll que respeta al lector. |
| Envíos y borradores | [web/storage.mjs](web/storage.mjs), [docs/v3-contract-inventory.md](docs/v3-contract-inventory.md) | Conservar hasta confirmación, no repetir envíos ambiguos. |
| Contratos verificables | [server/tests/test_contract_matrix.py](server/tests/test_contract_matrix.py), [web/v2/tests/](web/v2/tests/) | Recuperación, identidad, revisiones y comportamiento entre vistas. |

### Diferencias que la reescritura debe resolver

1. **Proyecto es una entidad nueva del producto:** necesita ubicación de código por nodo, almacenamiento, pertenencia obligatoria de salas, agrupación opcional y navegación compartida.
2. **Hoy hay conversaciones y varios tipos de salas:** el usuario de Ares debe tener una sola noción de sala; los adaptadores internos conservan las identidades y capacidades de cada origen.
3. **Actividad resumida no equivale a historial completo:** revisar persistencia de herramientas, salidas y eventos intermedios.
4. **Transporte de eventos no garantiza streaming completo:** verificar generación, entrega y renderizado por cada ruta de ejecución.
5. **Cuotas y descubrimiento dependen hoy de lógica del panel/helper:** definir su lugar en el servicio para operar también con el panel cerrado.
6. **Pausa y eliminación necesitan contratos propios:** la pausa debe descargar sesiones y reducir memoria en los nodos implicados; verificar las capacidades de Hermes y la recuperación del contexto antes de dar por resuelta esa función.
7. **Multimedia de extremo a extremo:** unificar adjuntos enviados y resultados del bot, resolver acceso a archivos entre nodos y dispositivos, y comprobar imágenes/videos en la interfaz elegida y en las rutas de entrega al modelo.
8. **Identidad visual integrada:** pasar de estilos parciales a un diseño coherente con Omarchy, tema compartido entre clientes y lenguaje común de componentes y animaciones. La calidad visual se trabaja desde el inicio.

Es especialmente útil [docs/plugin-room-removal-constraint.md](docs/plugin-room-removal-constraint.md): documenta límites de las salas hospedadas y una base de salas del plugin que todavía no cuenta con historial/ejecutor completo. No conviene elegir ese camino como solución terminada sin resolver esa brecha.

## 12. Respaldo, proyecto limpio e incorporación progresiva

### Paso obligatorio: respaldar todo lo actual

Antes de crear la implementación de Ares, conservar una copia completa del estado actual que permita recuperarlo y consultarlo durante la reescritura.

- Incluir el directorio completo del proyecto: código, documentación, este plan, pruebas, recursos gráficos, capturas, scripts, binarios y experimentos.
- Conservar el historial y metadatos de Git, los cambios sin commit, los archivos no versionados y los ignorados. Un commit o una copia de los archivos rastreados por Git no sustituye este respaldo.
- Inventariar y respaldar también la configuración y el estado persistente que la instalación actual utiliza fuera del repositorio: datos de salas/sesiones, borradores, adjuntos y configuración de servicios e integración del plugin, según sus ubicaciones reales. Registrar destinos de enlaces simbólicos y dependencias externas necesarias para recuperarla; no asumir que copiar el repositorio las incluye.
- Obtener copias consistentes de los datos que puedan estar cambiando durante el respaldo, especialmente bases de datos e historiales.
- Guardar el respaldo en una ubicación separada de los directorios de desarrollo, identificado por fecha y estado de origen. Documentar su contenido, ubicaciones originales y pasos de restauración; conservar los permisos de los datos privados.
- Verificar integridad y recuperación en una ubicación temporal antes de darlo por terminado. Cualquier parte faltante debe resolverse antes de comenzar la implementación.

**Resultado esperado:** poder volver al estado previo y consultar todos sus archivos, incluido el trabajo que todavía no estaba en Git. La ubicación concreta se definirá al ejecutar esta etapa. Este documento establece la obligación; el respaldo aún no se ha realizado.

### Comenzar Ares en un proyecto limpio

- Crear un directorio y repositorio nuevos para Ares, separados del proyecto actual y del respaldo.
- Comenzar con este plan y la estructura mínima necesaria para el nuevo producto.
- Mantener el proyecto original y su instalación como referencia recuperable durante la construcción. El entorno de desarrollo de Ares debe usar estado propio para no modificar datos actuales accidentalmente.
- Conservar las referencias de este documento mediante un mapa al proyecto original respaldado: las rutas de archivos existentes describen el origen, no archivos que deban existir desde el inicio en Ares.

### Montar solo lo que se requiera

Para cada capacidad del plan, identificar qué hace falta, revisar la referencia existente y decidir qué reutilizar, adaptar o escribir de nuevo. Incorporar una pieza acotada, comprobarla en el nuevo proyecto y continuar con la siguiente.

Las caras animadas, cuotas, integración con Hermes, descubrimiento de peers y contratos de eventos son candidatos concretos para aprovechar. Cada incorporación debe encajar en el modelo proyectos/salas, la nueva dirección visual y la gestión de sesiones de Ares. Registrar brevemente su origen y dependencias para que la nueva base permanezca comprensible.

**Regla de construcción:** respaldo completo primero; base limpia después; capacidades incorporadas progresivamente según necesidad. Preservar algo en el respaldo no obliga a trasladarlo al nuevo producto.

### Incorporación de conversaciones y datos existentes

**Propuesta a validar:** preservar las identidades e historiales actuales y asignar cada conversación/sala incorporada a un proyecto de Ares.

- Inventariar los orígenes existentes antes de migrar: chats individuales, salas Desktop y salas hospedadas.
- Convertir la experiencia de chat individual en sala de un bot, conservando la asociación con su sesión de origen.
- Elegir un proyecto de código de destino antes de incorporar una conversación como sala. Los elementos aún sin asignar pueden quedar en un inventario de importación pendiente, sin habilitarlos como chats sueltos. No crear un proyecto genérico «Importados» sin carpeta/repositorio.
- Evitar duplicaciones por nombres iguales: la asociación se basa en identidades y origen.
- Definir la incorporación de sesiones nuevas creadas fuera de Ares, no solo la migración inicial.
- Explicitar cuando un historial antiguo es parcial; no prometer herramientas o fragmentos que nunca fueron conservados.

El momento de importar conversaciones y datos existentes queda abierto: puede hacerse después de comenzar a usar nuevos proyectos de código en Ares. Esto es independiente de la decisión ya confirmada de construir la aplicación en un repositorio limpio. Ninguna opción debe introducir chats sin proyecto dentro de Ares.

## 13. Etapas propuestas y criterios de aceptación

Estas etapas ordenan el trabajo futuro; el paso actual es acordar el plan.

| Etapa | Resultado esperado | Cómo comprobarlo |
| --- | --- | --- |
| 0. Respaldo y base limpia — obligatoria antes de implementar | Respaldo completo del proyecto y estado externo relevante; nuevo directorio/repositorio para Ares con este plan y estructura mínima. | Verificar integridad y recuperación del respaldo, incluyendo trabajo sin commit y archivos no versionados/ignorados; comprobar separación del original y del estado de desarrollo. |
| Diseño transversal desde el inicio | Dirección visual Omarchy, bocetos de vistas principales y comportamiento animado, incluyendo multimedia y móvil. | Revisar coherencia con el escritorio, legibilidad con distintos temas y fluidez de las interacciones a medida que se construyen las vistas. |
| 1. Cerrar el modelo | Definir agrupaciones, ubicación de código, sala, participantes, descarga de sesiones, eliminación y registro global. | Recorrer creación, envío, pausa, reanudación y eliminación sin producir chats o ejecuciones huérfanos; identificar las capacidades reales de descarga de Hermes. |
| 2. Recorrido mínimo completo | Un proyecto, una sala, un bot, envío y actividad duradera en vivo. | Ver texto parcial y una herramienta antes de terminar; reabrir y encontrar la misma actividad. |
| 3. Paridad entre clientes y multimedia | Escritorio/widget y teléfono consumen la misma sala; imágenes y videos en ambos sentidos. | Ambos ven estados y eventos consistentes; imágenes ampliables y videos reproducibles en mensajes enviados y recibidos, conservados al reabrir; reconectar no duplica mensajes ni ejecuciones. |
| 4. Organización y bots | Grupos de proyectos, varias salas, pausa/eliminación, catálogo de bots y configuración. | Pausar reduce recursos residentes locales; leer historial no recarga sesiones y reanudar conserva contexto y borradores. |
| 5. Peers y colaboración | Conexión automática, salas con bots locales/remotos, multimedia entre nodos y pausa distribuida. | Verificar entrega y visualización de adjuntos/resultados remotos, medir descarga por nodo e informar pausa parcial ante desconexión sin reactivar salas pausadas ni mezclar trabajos. |
| 6. Pulido e incorporación | Completar el rediseño integrado con Omarchy, widget, notificaciones, cuotas actuales con separadores y estrategia de datos existentes. | Verificar coherencia de temas y animaciones entre clientes, conservar las funciones apreciadas y comprobar que cada elemento incorporado tiene proyecto y sala. |

Desde el primer recorrido completo, el widget y las notificaciones deben usar eventos reales. El diseño visual, el movimiento y la integración con Omarchy acompañan todas las etapas; el pulido final completa ese trabajo. La visibilidad del trabajo es una condición desde el comienzo.

## 14. Decisiones para la siguiente conversación

Prioridad para seguir escribiendo el plan:

Ya acordado: **proyecto = carpeta de código/repositorio**; debe poder agruparse con proyectos similares. **Pausar = reducir memoria de las sesiones**, tanto locales como remotas, conservando el trabajo para retomarlo.

También acordado: **imágenes y videos en ambos sentidos dentro de la sala**, y **rediseño visual integrado con Omarchy, animado y fresco**. Las propuestas de pantallas y sus detalles se desarrollarán a partir de esa dirección.

La construcción comienza con **respaldo completo verificado → proyecto limpio separado → incorporación gradual de lo necesario**. Es una decisión confirmada, pendiente de ejecución cuando se inicie la implementación.

1. **¿Qué comparten los proyectos de un grupo?** ¿Solo organización o también instrucciones, convenciones y configuración?
2. **Gestión de sesiones:** necesidad acordada; los detalles de pausa, descarga y reanudación quedan para el diseño técnico y no requieren una decisión de producto ahora.
3. **¿Qué significa eliminar?** ¿Papelera/recuperación o borrado definitivo del historial?
4. **¿Cada sala puede tener uno o varios bots?** La propuesta es unificar ambos casos.
5. **¿Dónde vive la actividad global?** Definir su ubicación sin introducir proyectos ficticios ajenos al código ni chats sueltos.

Después: elección de interfaz principal de escritorio, configuración de modelo por bot/sala, incorporación de conversaciones existentes y detalles de la referencia diaria de cuotas.
