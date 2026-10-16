# Actualités — informatique biologique quantique

Une petite application de bureau Windows qui rassemble des nouvelles sur la
biologie quantique, la bioinformatique et les applications biologiques de
l’informatique quantique. Elle cherche dans les éditions Google Actualités des
États-Unis, du Royaume-Uni, du Canada (en anglais et en français) et de
l’Australie, puis affiche uniquement les articles publiés au cours du dernier
mois calendaire. Les articles dont la date de publication manque ou dépasse
cette limite sont exclus. Les sources peuvent être en anglais; les résumés
générés par Gemini sont en français. Les résultats sont classés par date de
publication, du plus récent au plus ancien; la pertinence départage les
articles publiés à la même date. Les flux RSS sont destinés à un usage
personnel comme lecteur d’actualités.

## Construire l’exécutable Windows

Dans PowerShell, depuis ce dossier, construisez l’exécutable autonome :

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1 `
  -PythonExe "C:\Users\Utilisateur\AppData\Local\Python\pythoncore-3.15-64\python.exe"
```

La première construction télécharge PyInstaller. Le fichier prêt à lancer se
trouve dans `dist\mistral-gemini-groq\ActualitesBiologieQuantique.exe`. Double-cliquez
dessus pour ouvrir le widget manuellement; il cherche les nouvelles au
démarrage. Le nouveau fichier est placé dans un dossier séparé afin de pouvoir
le construire même si une ancienne version du widget est encore ouverte.
Aucune installation de Python n’est requise pour utiliser le fichier construit.
Au lancement manuel, une barre animée indique la progression de la recherche
et des résumés. Pour l’ouverture automatique, la fenêtre reste cachée jusqu’à
ce que le contenu soit prêt. Le bouton **Actualiser** lance une nouvelle
recherche et affiche également la progression.

## Ouvrir le widget automatiquement

Après avoir construit l’exécutable, enregistrez la tâche Windows :

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
```

Le widget s’ouvre à 11 h, heure locale, lorsque la session Windows est ouverte.
Si l’ordinateur démarre ou si vous vous connectez après 11 h, il s’ouvre à la
prochaine connexion. Si la connexion a lieu avant 11 h, il attend 11 h. Il ne
s’ouvre qu’une fois par jour via la tâche automatique. Windows peut réveiller
l’ordinateur pour exécuter la tâche lorsqu’il est en veille. En revanche, si
l’ordinateur est éteint à 11 h, l’ouverture a lieu à la prochaine connexion.

Pour retirer la tâche automatique :

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1
```

L’exécutable et la tâche sont indépendants de l’interpréteur Python après la
construction. Si vous déplacez ou supprimez le dossier du projet, reconstruisez
la tâche avec `install_task.ps1` en indiquant l’emplacement de l’exécutable :

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 `
  -ExecutablePath "C:\chemin\vers\ActualitesBiologieQuantique.exe"
```

## Résumés par IA : Mistral, Gemini, puis Groq

L’application essaie les fournisseurs dans cet ordre : Mistral, Gemini, puis
Groq. Elle passe au suivant si la clé est absente ou si un appel échoue.
Dans **Variables d’environnement de votre compte** dans Windows, configurez
les clés que vous possédez :

```text
MISTRAL_API_KEY = votre-cle-mistral
GEMINI_API_KEY  = votre-cle-gemini
GROQ_API_KEY    = votre-cle-groq
```

Les modèles par défaut sont `mistral-small-latest`, `gemini-3.8-flash` et
`llama-3.3-70b-versatile`. Pour personnaliser les modèles Mistral, Gemini et
Groq, définissez respectivement `MISTRAL_MODEL`, `NEWS_AI_MODEL` et
`GROQ_MODEL`.

Vous pouvez ouvrir ce réglage en recherchant « Modifier les variables
d’environnement de votre compte » dans le menu Démarrer. Après l’avoir
enregistré, fermez et rouvrez votre session Windows pour que la tâche
automatique voie la nouvelle configuration. Le lancement manuel de
`dist\mistral-gemini-groq\ActualitesBiologieQuantique.exe` utilisera aussi ces variables.
Ne collez pas la clé dans le code ni dans une commande qui resterait dans
l’historique du terminal.

Pour lancer l’application Python depuis PowerShell, vous pouvez aussi définir
temporairement les variables pour cette fenêtre :

```powershell
$env:MISTRAL_API_KEY = Read-Host "Collez votre clé Mistral"
$env:GEMINI_API_KEY = Read-Host "Collez votre clé Gemini de secours"
$env:GROQ_API_KEY = Read-Host "Collez votre clé Groq de secours"
python .\app.py
```

Pour chaque article, l’application résout d’abord les liens intermédiaires de
Google Actualités vers la page de l’éditeur, en extrait les paragraphes
accessibles et les transmet au modèle pour produire un résumé plus détaillé.
Les paywalls et les pages qui bloquent l’accès automatique utilisent l’extrait
RSS à la place et sont signalés dans la section des avertissements. Le texte
de chaque page est limité à 10 000 caractères et son téléchargement à 1 Mo.
En cas d’erreur Mistral, l’application tente Gemini, puis Groq si Gemini
échoue aussi. Si tous les fournisseurs configurés échouent, elle indique que
le résumé français est indisponible et affiche l’extrait original en secours;
cet extrait peut être en anglais. Les clés sont envoyées aux fournisseurs
respectifs par HTTPS; elles ne sont pas incluses dans le fichier `.exe`.
Une erreur HTTP 403 de Groq signifie que le serveur refuse l’accès : vérifiez
que la clé est active et que le bon compte/projet a accès à l’API et au modèle.
Le code Cloudflare 1010 signale plutôt un blocage de la requête selon la
signature du client; il ne prouve pas que la clé ou le projet est incorrect.
Essayez un autre réseau, sans VPN ni proxy si possible. Si le blocage persiste,
contactez le support Groq avec le Ray ID s’il est disponible, jamais avec la clé.
Un 429 indique une limite ou un quota atteint; un 503 signale une indisponibilité
temporaire du service.

Si une clé a été partagée dans une conversation ou publiée, révoquez-la dans
Google AI Studio et configurez une nouvelle clé localement. Pour continuer à
utiliser un service compatible avec l’API OpenAI, définissez
`NEWS_AI_PROVIDER=openai` et `NEWS_AI_API_KEY`; `NEWS_AI_MODEL` prend alors
par défaut la valeur `gpt-4o-mini`. Un endpoint compatible peut être configuré
avec `NEWS_AI_BASE_URL`.

## Tests

```powershell
python -m unittest discover -s tests -v
```
