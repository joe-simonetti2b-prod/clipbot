# clipbot

Bot de clipping autonome : il surveille des lives (Twitch, Kick, YouTube),
détecte les moments de hype dans le chat, les monte en vertical 9:16 avec
sous-titres animés et accroche, écrit la légende, puis te les envoie sur
Telegram pour validation en un tap, ou les publie directement sur TikTok.

Il tourne 24 h/24 **gratuitement** dans le cloud. Tout se pilote depuis le téléphone.

```
Lives ──▶ chat ──▶ détection de hype ──▶ tampon 5 min ──▶ clip brut
                                                              │
   TikTok ◀── ✅ Telegram ◀── légende IA ◀── 9:16 + sous-titres ◀┘
```

## Hébergement gratuit : deux options

| | **Oracle Cloud Always Free** (recommandé) | **Render Free** (plan B) |
|---|---|---|
| Coût | 0 € à vie | 0 € |
| Carte bancaire | demandée pour vérifier l'identité, non débitée | non |
| Puissance | 2 cœurs ARM, jusqu'à 12 Go RAM, 200 Go disque | très petite machine, 512 Mo |
| Lives suivis en même temps | 3 | 1 |
| Qualité de sortie | 1080×1920 | 720×1280 |
| Données (connexion TikTok, file de clips) | conservées | perdues à chaque redéploiement |
| Mises à jour | automatiques (vérifie GitHub toutes les 10 min) | automatiques à chaque push |

Hugging Face Spaces n'est pas proposé : d'après les retours de la communauté,
les nouveaux Spaces n'ont plus accès au CPU gratuit depuis l'été 2026.

### Option A — Oracle Cloud (≈ 15 min, aucune commande à taper)

1. Crée un compte sur **cloud.oracle.com** (offre *Free Tier*).
2. Menu → *Compute* → *Instances* → **Create instance** :
   - *Image* : **Ubuntu 24.04**
   - *Shape* : **Ampere VM.Standard.A1.Flex**, 2 OCPU, **4 Go** de RAM
     (4 Go plutôt que 12 : Oracle récupère les machines « inactives »
     qui utilisent moins de 20 % de CPU, réseau ET mémoire pendant 7 jours ;
     avec 4 Go, le bot dépasse naturellement ce seuil de mémoire)
   - *Show advanced options* → *Management* → **Paste cloud-init script** :
     colle le contenu de `deploy/oracle-cloud-init.sh` et remplis les
     lignes du haut (dépôt, token Telegram, code, chaînes).
3. **Create**. Au bout de ~10 min, le bot t'écrit sur Telegram.

Si Oracle affiche « Out of capacity », change de *Availability domain* ou
réessaie plus tard : les machines ARM gratuites sont très demandées.

### Option B — Render (≈ 5 min, sans carte)

1. **render.com** → *Sign in with GitHub*.
2. *New* → **Blueprint** → choisis le dépôt `clipbot` (Render lit `render.yaml`).
3. Remplis les 4 champs demandés : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_PAIR_CODE`,
   `ALLOWED_CHANNELS`, `GROQ_API_KEY` (clé gratuite sur console.groq.com —
   la machine est trop petite pour transcrire elle-même).
4. Après l'appairage Telegram, ajoute `TELEGRAM_OWNER_ID` (le bot te donne
   la valeur) pour ne jamais refaire `/start`.

Le bot se réveille lui-même toutes les 10 min pour que Render ne l'endorme pas
(Render accorde 750 h gratuites/mois, assez pour un service 24 h/24).

### Appairage (les deux options)

Ouvre ton bot Telegram → `/start <ton code>` → `/status`.
Chaque clip arrive ensuite avec ✅ Publier / ❌ Jeter.

## Chaînes Twitch et Kick

`ALLOWED_CHANNELS` (ou `/add`) accepte un simple pseudo : le bot le cherche sur **Twitch
et Kick**, le garde partout où il existe, et t'envoie sur Telegram la liste des pseudos
introuvables. `kick:pseudo` ou `twitch:pseudo` forcent une plateforme.

`FOCUS_CHANNELS` : créateurs prioritaires (ex. la scène française). Leurs viewers comptent
`FOCUS_BOOST` fois (3 par défaut) : un live FR à 20 000 viewers passe devant un live US à 50 000.

Toutes les 5 minutes, le bot compare le nombre de viewers de toutes les chaînes en live
et suit les **2 plus regardées**. La rotation est progressive : un live suivi n'est
remplacé que par un live 1,3× plus regardé, après au moins 10 minutes de suivi, et un
seul changement par cycle. La file de montage alterne entre les créateurs.

## Commandes Telegram

| Commande | Effet |
|---|---|
| `/status` | lives suivis, clips du jour, état TikTok |
| `/add kamet0` · `/add kick:xxx` | suivre une chaîne |
| `/remove …` · `/chaines` | gérer la liste |
| `/auto on` / `off` | publier sans validation |
| `/lives` | choisir à la main les lives suivis (boutons) ; `/suivre pseudo` ; `/algo` pour revenir en auto |
| `/tag @compte` · `/outro on/off` | tag incrusté sur les vidéos · fin « S'abonner / Partager » |
| `/relance` | renvoyer tout de suite la file vers TikTok |
| `/pause` · `/resume` | couper / relancer la surveillance |
| `/tiktok` | lien de connexion TikTok (une fois) |

## Options à activer plus tard

| Option | Ce que ça apporte | Où l'obtenir |
|---|---|---|
| `GROQ_API_KEY` | transcription dans le cloud, gratuite et rapide (libère le CPU) | console.groq.com |
| `TWITCH_CLIENT_ID/SECRET` | veille par tendances (top lives FR, Sports, Just Chatting) au lieu d'une liste fixe | dev.twitch.tv/console → *Register your application* (2FA Twitch requise) |
| `ANTHROPIC_API_KEY` | accroche + légende + hashtags écrits par l'IA, **et tri automatique** des clips faibles | console.anthropic.com |
| `TIKTOK_CLIENT_KEY/SECRET` | envoi direct vers TikTok | developers.tiktok.com (voir ci-dessous) |
| `CHANNEL_TAGS` | mentions exigées par chaque campagne de clipping, ajoutées à la légende. Ex : `xqc=@xqc #xqcclips; kamet0=@kamet0` | règles de la campagne (Whop…) |
| `VIDEO_CREDIT=false` | retire le crédit « twitch.tv/chaîne » incrusté en bas de la vidéo (activé par défaut) | — |
| `LONG_CLIPS=true` | clips de ~65 s (éligibilité aux fonds créateurs > 1 min) | — |

**App TikTok** (nécessite une adresse https publique : fournie d'office par
Render ; sur Oracle, on l'ajoutera ensemble le moment venu) :
developers.tiktok.com → *Manage apps* → *Connect an app*.
Ajoute les produits **Login Kit** et **Content Posting API**. Renseigne :
- Redirect URI : `https://<ton-domaine>/tiktok/callback`
- Terms of Service URL : `https://<ton-domaine>/terms`
- Privacy Policy URL : `https://<ton-domaine>/privacy`

Soumets l'app, puis tape `/tiktok` dans Telegram une fois qu'elle est validée.
- Avant l'audit TikTok (`TIKTOK_MODE=inbox`) : les clips arrivent en brouillon
  dans ton app TikTok, tu touches *Publier*. Limite : 5 brouillons par jour.
- Après l'audit (`TIKTOK_MODE=direct`) : publication publique 100 % automatique.

Tant que TikTok n'est pas branché, publie depuis Telegram : ouvre la vidéo →
⋮ → *Enregistrer dans la galerie* → TikTok. La légende est dans le message.

## Rentabilité : réglages clés (v2)

| Variable | Effet | Valeur conseillée |
|---|---|---|
| `GROQ_API_KEY` | transcription **et** IA gratuite : note chaque clip, écrit accroche + légende, traduit | ta clé Groq |
| `TARGET_LANG` | sous-titres traduits quand le live est dans une autre langue (streamers US → FR) | `fr` |
| `MIN_HYPE_SCORE` | pics trop faibles ignorés dès la détection | `5` |
| `MIN_AI_SCORE` | clips notés en dessous par l'IA : jetés **avant** le montage (CPU économisé) | `5` |
| `MAX_BACKLOG` | nombre de clips en attente de montage ; au-delà, seuls les meilleurs restent | `3` |
| `LONG_CLIPS` | clips de ~65 s (fonds créateurs > 1 min) | `true` |
| `CHANNEL_TAGS` | mentions exigées par les campagnes de clipping, par chaîne | voir `.env.example` |

### YouTube Shorts

1. console.cloud.google.com → nouveau projet → *API et services* → activer **YouTube Data API v3**.
2. *Écran de consentement OAuth* : type **Externe**, puis statut de publication **En production**
   (sinon Google coupe la connexion au bout de 7 jours). Ignore l'avertissement « app non validée ».
3. *Identifiants* → *Créer* → **ID client OAuth** → type *Application Web* →
   URI de redirection : `https://<ton-service>.onrender.com/youtube/callback`.
4. Colle `YOUTUBE_CLIENT_ID` et `YOUTUBE_CLIENT_SECRET` dans Render, puis `/youtube` dans Telegram.

Limites Google : 6 Shorts par jour (quota gratuit), et vidéos forcées en privé tant que
le projet n'a pas passé l'audit de l'API YouTube (formulaire gratuit dans la console).

## Architecture

| Fichier | Rôle |
|---|---|
| `discovery.py` | veille : API Twitch / YouTube / Kick, ou sonde yt-dlp sans clé pour les chaînes listées |
| `chat.py` | chat en direct : IRC Twitch anonyme, polling YouTube, webhook Kick |
| `hype.py` | détection des pics : z-score adaptatif × rire / action / « clip it » / diversité |
| `recorder.py` | tampon circulaire yt-dlp → FFmpeg (copie, ≈ 0 CPU) et extraction du brut |
| `llm.py` / `translate.py` | IA (Claude ou Groq gratuit) : note, textes, traduction des sous-titres |
| `youtube.py` | publication YouTube Shorts (OAuth Google, envoi reprenable, quota suivi) |
| `transcribe.py` | Whisper local (faster-whisper int8), horodatage au mot |
| `copywriter.py` | accroche, légende, hashtags, note de viralité (Claude) ; crédit du streamer forcé |
| `layout.py` | choix auto du cadrage : visage centré, facecam + jeu, ou fond flouté |
| `subtitles.py` / `render.py` | sous-titres karaoké + montage 1080×1920, son à −14 LUFS, en une passe |
| `pipeline.py` | file SQLite : montage → validation → publication → ménage |
| `telegram.py` / `tiktok.py` | télécommande et publication officielle |
| `web.py` / `app.py` | serveur HTTP, assemblage, arrêt propre sur SIGTERM |

## Limites

- YouTube bloque souvent le téléchargement depuis les serveurs cloud : priorité à Twitch.
- Droits : clippe des chaînes qui l'autorisent (programmes de clipping, accord
  écrit). Les retransmissions sportives officielles déclenchent des
  réclamations automatiques et des bannissements.

## Développement

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 23 tests hors-ligne
python -m clipbot --check    # vérifie outils et variables
```
