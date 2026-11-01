// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for French (`fr`).
class AppLocalizationsFr extends AppLocalizations {
  AppLocalizationsFr([String locale = 'fr']) : super(locale);

  @override
  String get appName => 'Polaris';

  @override
  String get appSub => 'Workforce';

  @override
  String get otherLanguage => 'EN';

  @override
  String get switchLanguage => 'Switch to English';

  @override
  String get statusPresent => 'Présent';

  @override
  String get statusAbsent => 'Absent';

  @override
  String get statusHalfDay => 'Demi-journée';

  @override
  String get statusLate => 'Retard';

  @override
  String get statusLetterPresent => 'P';

  @override
  String get statusLetterAbsent => 'A';

  @override
  String get statusLetterHalfDay => '½';

  @override
  String get statusLetterLate => 'R';

  @override
  String get phoneTitle => 'Connexion';

  @override
  String get phoneLabel => 'Numéro de téléphone';

  @override
  String get continueButton => 'Continuer';

  @override
  String get pinPrompt => 'Saisissez votre code à 4 chiffres';

  @override
  String get pinEntered => 'Code saisi';

  @override
  String pinDotsLabel(int count) {
    return '$count chiffres sur 4 saisis';
  }

  @override
  String get notYou => 'Ce n’est pas vous ?';

  @override
  String foremanAt(String site) {
    return 'Chef d’équipe · $site';
  }

  @override
  String get signInButton => 'Ouvrir l’appel du jour';

  @override
  String get keyClear => 'Effacer';

  @override
  String get keyDelete => 'Supprimer';

  @override
  String wrongPin(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'Encore $count essais',
      one: 'Encore 1 essai',
    );
    return 'Code incorrect. $_temp0.';
  }

  @override
  String get wrongPinNoCount => 'Numéro ou code incorrect.';

  @override
  String lockedUntil(String time) {
    return 'Trop de codes incorrects. Réessayez à $time.';
  }

  @override
  String get lockedNoTime => 'Trop de codes incorrects. Réessayez plus tard.';

  @override
  String throttled(int seconds) {
    return 'Trop d’essais. Attendez $seconds s.';
  }

  @override
  String get signInOffline => 'Pas de réseau. La connexion en a besoin.';

  @override
  String signInError(String code) {
    return 'Connexion impossible ($code).';
  }

  @override
  String get sessionExpired =>
      'Votre session a expiré. Saisissez votre code. Le travail non envoyé reste sur ce téléphone.';

  @override
  String get bannerOffline => 'Hors ligne';

  @override
  String get bannerWorksNormally => 'tout marche';

  @override
  String get bannerOnline => 'En ligne';

  @override
  String get bannerAllSent => 'tout est envoyé';

  @override
  String get bannerSending => 'Envoi…';

  @override
  String get bannerChecking => 'Vérification du réseau…';

  @override
  String bannerWaiting(int count) {
    return '$count en attente';
  }

  @override
  String bannerFailed(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count non envoyés · touchez pour voir',
      one: '1 non envoyé · touchez pour voir',
    );
    return '$_temp0';
  }

  @override
  String morningDue(String time) {
    return 'Matin · avant $time';
  }

  @override
  String get morning => 'Matin';

  @override
  String get rollCall => 'Appel';

  @override
  String crewCount(int count) {
    return '$count ouvriers dans votre équipe';
  }

  @override
  String get startRollCall => 'Commencer l’appel';

  @override
  String get continueRollCall => 'Continuer l’appel';

  @override
  String get viewRollCall => 'Voir l’appel';

  @override
  String get fixRollCall => 'Corriger et renvoyer';

  @override
  String sentBack(String note) {
    return 'Renvoyé : $note';
  }

  @override
  String get dayNotStarted => 'Pas commencé';

  @override
  String dayDraft(int marked, int total) {
    return '$marked sur $total marqués';
  }

  @override
  String get daySubmitted => 'Envoyé';

  @override
  String get dayApproved => 'Validé';

  @override
  String get dayRejected => 'Renvoyé';

  @override
  String get dayLocked => 'Clôturé';

  @override
  String get dayPaid => 'Payé';

  @override
  String get evening => 'Soir';

  @override
  String get cashPayout => 'Paie en espèces';

  @override
  String get payoutAfterApproval => 'Après validation';

  @override
  String get payoutOpensText =>
      'S’ouvre quand le conducteur de travaux valide l’appel du jour.';

  @override
  String get previewPayout => 'Voir la paie de ce soir';

  @override
  String get payoutOpen => 'Ouverte';

  @override
  String payoutOpenText(int count, String amount) {
    return '$count à payer · reste $amount';
  }

  @override
  String get startPayout => 'Commencer la paie';

  @override
  String get payoutPaidPill => 'Tous payés';

  @override
  String get payoutClosed => 'Clôturée';

  @override
  String get viewPayout => 'Voir la paie';

  @override
  String get thisWeek => 'Cette semaine';

  @override
  String get signOut => 'Se déconnecter';

  @override
  String get chooseSite => 'Choisir un chantier';

  @override
  String signOutWaiting(int count) {
    return '$count changements ne sont pas encore envoyés. Ils restent sur ce téléphone et partiront après la reconnexion.';
  }

  @override
  String get cancel => 'Annuler';

  @override
  String get loading => 'Chargement…';

  @override
  String get noSite =>
      'Aucun chantier ne vous est attribué. Demandez au bureau.';

  @override
  String rollCallSubtitle(String site, String date) {
    return '$site · $date · enregistré sur le téléphone';
  }

  @override
  String tabToMark(int count) {
    return 'À marquer · $count';
  }

  @override
  String tabAll(int count) {
    return 'Tous $count';
  }

  @override
  String get allPresent => 'Tous P';

  @override
  String get allPresentHint => 'Marquer présents tous ceux qui restent';

  @override
  String get reviewSubmit => 'Vérifier et envoyer';

  @override
  String reviewNotMarked(int count) {
    return 'Vérifier · $count non marqués';
  }

  @override
  String statusFor(String name) {
    return 'Statut de $name';
  }

  @override
  String get submittedReadOnly =>
      'Envoyé. Toute modification demande un motif.';

  @override
  String get lockedReadOnly => 'Clôturé. Plus de modification possible.';

  @override
  String get everyoneMarked => 'Tout le monde est marqué.';

  @override
  String get reasonTitle => 'Pourquoi ce changement ?';

  @override
  String get reasonHint => 'Motif (obligatoire)';

  @override
  String get save => 'Enregistrer';

  @override
  String get arrival => 'Arrivée';

  @override
  String get departure => 'Départ';

  @override
  String get noCrew =>
      'Pas encore de liste d’équipe sur ce téléphone. Connectez-vous une fois au réseau pour la télécharger.';

  @override
  String get reviewTitle => 'Vérifier et envoyer';

  @override
  String get exceptionsTitle => 'Journées incomplètes · à vérifier';

  @override
  String get change => 'Modifier';

  @override
  String halfDayTimes(String from, String to) {
    return 'Demi-journée · $from–$to';
  }

  @override
  String halfDayLeaves(String time) {
    return 'Demi-journée · part à $time';
  }

  @override
  String lateArrived(String time) {
    return 'Retard · arrivé à $time';
  }

  @override
  String absentStreak(int count) {
    return 'Absent · $count jours de suite';
  }

  @override
  String get absentToday => 'Absent aujourd’hui';

  @override
  String moreAbsent(int count) {
    return '+ $count autres absents';
  }

  @override
  String get noteLabel => 'Note pour le conducteur de travaux';

  @override
  String get optional => 'Facultatif';

  @override
  String get offlineSubmitTitle => 'Vous êtes hors ligne.';

  @override
  String get offlineSubmitBody =>
      'L’envoi enregistre l’appel sur ce téléphone et le transmet dès qu’il y a du réseau. Vous pouvez continuer.';

  @override
  String submitCount(int count) {
    return 'Envoyer $count ouvriers';
  }

  @override
  String submittedAt(String time) {
    return 'Envoyé à $time.';
  }

  @override
  String submittedWaiting(String manager) {
    return 'En attente d’envoi (hors ligne) — puis $manager le valide. Toute modification demande désormais un motif.';
  }

  @override
  String submittedSent(String manager) {
    return 'Transmis — $manager le valide ensuite. Toute modification demande désormais un motif.';
  }

  @override
  String get theSiteManager => 'le conducteur de travaux';

  @override
  String get backToToday => 'Retour à aujourd’hui';

  @override
  String notMarkedWarning(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count ouvriers ne sont pas encore marqués.',
      one: '1 ouvrier n’est pas encore marqué.',
    );
    return '$_temp0';
  }

  @override
  String get noExceptions => 'Tout le monde a fait une journée complète.';

  @override
  String get payoutTitle => 'Paie en espèces';

  @override
  String payoutSubtitleApproved(String site, String date, String time) {
    return '$site · $date · validé à $time';
  }

  @override
  String payoutSubtitlePreview(String site, String date) {
    return '$site · $date · aperçu';
  }

  @override
  String get inHand => 'En main';

  @override
  String get paid => 'Payés';

  @override
  String get left => 'Reste';

  @override
  String get earnedToday => 'Gagné aujourd’hui';

  @override
  String get advanceRepayment => 'Remboursement d’avance';

  @override
  String get handOver => 'À remettre';

  @override
  String get proofOfReceipt => 'Preuve de remise';

  @override
  String get proofPhoto => 'Photo';

  @override
  String get proofSignature => 'Signature';

  @override
  String get proofThumb => 'Empreinte';

  @override
  String get proofNone => 'Sans preuve';

  @override
  String confirmPaid(String amount) {
    return 'Confirmer payé · $amount';
  }

  @override
  String get everyonePaid => 'Tout le monde est payé';

  @override
  String returnCash(String amount) {
    return 'Rendez $amount au bureau. La feuille est clôturée quand le bureau a compté l’argent.';
  }

  @override
  String get queue => 'File';

  @override
  String get queuePaid => 'Payé';

  @override
  String get queueNow => 'Maintenant';

  @override
  String get queueToPay => 'À payer';

  @override
  String previewBanner(String manager) {
    return 'Aperçu. La paie s’ouvre quand $manager valide l’appel.';
  }

  @override
  String get proofReady => 'Preuve prête';

  @override
  String get redoProof => 'Refaire';

  @override
  String get takeProofFirst => 'Prenez d’abord la preuve';

  @override
  String get signHere => 'Demandez à l’ouvrier de signer';

  @override
  String get thumbHere => 'Demandez à l’ouvrier de poser le pouce sur l’écran';

  @override
  String get clear => 'Effacer';

  @override
  String get done => 'Valider';

  @override
  String get notSentYet => 'pas encore envoyé';

  @override
  String get noPayout =>
      'Pas encore de feuille de caisse sur ce téléphone. Connectez-vous au réseau pour la charger.';

  @override
  String get photoFailed => 'Impossible de prendre la photo.';

  @override
  String get payoutClosedText => 'Le bureau a clôturé cette feuille.';

  @override
  String get failedTitle => 'Non envoyés';

  @override
  String get failedHint =>
      'Le serveur a refusé ces changements. Voyez avec le bureau.';

  @override
  String get discard => 'Supprimer';

  @override
  String kindRecords(String date) {
    return 'Marques d’appel · $date';
  }

  @override
  String kindSubmit(String date) {
    return 'Envoi de l’appel · $date';
  }

  @override
  String kindCorrection(String date) {
    return 'Correction · $date';
  }

  @override
  String kindPayment(String date) {
    return 'Paiement · $date';
  }

  @override
  String get mockOfflineOn => 'Mock : hors ligne';

  @override
  String get mockOfflineOff => 'Mock : en ligne';

  @override
  String mockKioskOn(String site) {
    return 'Démo : ce téléphone est maintenant la borne de $site. Appui long sur le nom du chantier pour l’arrêter.';
  }

  @override
  String get mockKioskOff => 'Démo : mode borne arrêté.';

  @override
  String kindCheckIn(String date) {
    return 'Pointage · $date';
  }

  @override
  String get scanBadges => 'Scanner les badges';

  @override
  String scanBadgesCount(int count) {
    return 'Scanner les badges · $count aujourd’hui';
  }

  @override
  String get scanTitle => 'Scanner les badges';

  @override
  String scanCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count scans',
      one: '1 scan',
      zero: 'Aucun scan',
    );
    return '$_temp0';
  }

  @override
  String get directionIn => 'Entrée';

  @override
  String get directionOut => 'Sortie';

  @override
  String get photoWithScan => 'Prendre une photo à chaque scan';

  @override
  String get todayScans => 'Scans du jour';

  @override
  String get backToRollCall => 'Retour à l’appel';

  @override
  String get simulateScan => 'Simuler un scan (mock)';

  @override
  String get scanQueued => 'Enregistré sur le téléphone · pas encore envoyé';

  @override
  String get scanSent => 'Envoyé';

  @override
  String get badgeNotRecognisedTitle => 'Badge non reconnu';

  @override
  String get badgeNotRecognised => 'Badge non reconnu — voyez le chef d’équipe';

  @override
  String get badgeRevoked => 'Ce badge n’est plus valable';

  @override
  String get notOnCrew =>
      'Pas dans l’équipe du jour — prévenez le chef d’équipe';

  @override
  String get outsideSite => 'Hors du chantier';

  @override
  String get alreadyCheckedIn => 'Entrée déjà pointée';

  @override
  String get alreadyCheckedOut => 'Sortie déjà pointée';

  @override
  String workerNumber(int id) {
    return 'Ouvrier n° $id';
  }

  @override
  String gpsReady(int metres) {
    return 'GPS actif · ±$metres m';
  }

  @override
  String get gpsReadyNoAccuracy => 'GPS actif';

  @override
  String get gpsLooking => 'Recherche du GPS…';

  @override
  String get gpsDenied =>
      'Pas d’accès au GPS : les scans partent sans position';

  @override
  String get gpsOff =>
      'La localisation est coupée : les scans partent sans position';

  @override
  String get cameraDenied =>
      'Autorisez l’appareil photo dans les réglages du téléphone pour scanner les badges.';

  @override
  String get cameraError => 'L’appareil photo ne démarre pas.';

  @override
  String get selfCheckInNote =>
      'Les ouvriers pointent eux-mêmes : l’appel montre ce que leurs scans ont écrit. Vérifiez et envoyez.';

  @override
  String prefilledNote(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count marques viennent des scans de badge. Vérifiez-les.',
      one: '1 marque vient des scans de badge. Vérifiez-la.',
    );
    return '$_temp0';
  }

  @override
  String get fromCheckIn => 'D’après le badge';

  @override
  String get kioskTitle => 'Borne de pointage';

  @override
  String get kioskHint => 'Présentez votre badge';

  @override
  String get switchCamera => 'Changer de caméra';

  @override
  String get kioskExit => 'Quitter la borne';

  @override
  String get kioskExitText =>
      'Ce téléphone reste une borne tant que le bureau n’arrête pas le mode borne. Se déconnecter ?';

  @override
  String get kioskPinTitle => 'Code PIN du chef d’équipe';

  @override
  String get kioskPinWrong => 'Code PIN incorrect';

  @override
  String kindConsent(String date) {
    return 'Réponse au suivi · $date';
  }

  @override
  String get trackingConsentTitle => 'Suivi de la position';

  @override
  String get trackingConsentIntro =>
      'Votre entreprise utilise Polaris pour savoir qui est sur ses chantiers. Lisez comment cela fonctionne, puis choisissez.';

  @override
  String get trackingConsentWhenTitle => 'Quand';

  @override
  String trackingConsentWhenHours(String start, String end) {
    return 'Seulement pendant les heures de suivi, de $start à $end (heure du chantier), et seulement quand vous êtes connecté sur ce téléphone. En dehors de ces heures, rien n’est enregistré.';
  }

  @override
  String get trackingConsentWhenGeneric =>
      'Seulement pendant les heures de suivi fixées par votre entreprise, et seulement quand vous êtes connecté sur ce téléphone. En dehors de ces heures, rien n’est enregistré.';

  @override
  String get trackingConsentWhatTitle => 'Ce qui est enregistré';

  @override
  String trackingConsentWhatText(String every) {
    return 'La position du téléphone $every, avec sa précision, et les badges des ouvriers que le téléphone capte à proximité quand l’application est ouverte sur un chantier. Jamais vos appels, messages, photos ou autres applications.';
  }

  @override
  String trackingEveryMinutes(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'environ toutes les $count minutes',
      one: 'environ toutes les minutes',
    );
    return '$_temp0';
  }

  @override
  String trackingEverySeconds(int count) {
    return 'environ toutes les $count secondes';
  }

  @override
  String get trackingEveryGeneric => 'à intervalles réguliers';

  @override
  String get trackingConsentKeepTitle => 'Durée de conservation';

  @override
  String trackingConsentKeepDays(int days) {
    return 'Les positions brutes sont effacées après $days jours. Les heures d’arrivée et de départ sont conservées comme les présences.';
  }

  @override
  String get trackingConsentKeepGeneric =>
      'Les positions brutes sont effacées après la durée fixée par votre entreprise (30 jours sauf réglage différent). Les heures d’arrivée et de départ sont conservées comme les présences.';

  @override
  String get trackingConsentWhoTitle => 'Qui y a accès';

  @override
  String get trackingConsentWhoText =>
      'Les chefs de chantier voient qui est sur leurs propres chantiers. Les trajets entre chantiers ne sont visibles que par les administrateurs de l’entreprise. Vous pouvez demander votre propre historique au bureau.';

  @override
  String get trackingConsentUseTitle => 'À quoi cela sert';

  @override
  String get trackingConsentUseText =>
      'La présence ne fait que suggérer le pointage : l’appel est toujours confirmé par une personne. Elle ne sert jamais à sanctionner automatiquement.';

  @override
  String get trackingConsentChoice =>
      'Vous pouvez refuser : l’application fonctionne de la même façon, sans suivi. Vous pourrez changer d’avis plus tard.';

  @override
  String get trackingConsentAccept => 'Accepter';

  @override
  String get trackingConsentRefuse => 'Refuser';

  @override
  String trackingConsentVersion(String version) {
    return 'Version de la notice $version';
  }

  @override
  String get trackingConsentSaved =>
      'Réponse enregistrée. Elle sera envoyée dès que le téléphone sera en ligne.';

  @override
  String get trackingConsentNeeded => 'Suivi de la position : lisez la notice';

  @override
  String get trackingMenu => 'Suivi de la position';

  @override
  String get trackingOn => 'Position activée';

  @override
  String get trackingPaused => 'Position en pause';

  @override
  String get trackingOff => 'Position désactivée';

  @override
  String trackingOnDetail(String start, String end) {
    return 'Votre position est enregistrée pendant les heures de suivi, de $start à $end (heure du chantier).';
  }

  @override
  String trackingPausedDetail(String start, String end) {
    return 'En dehors des heures de suivi (de $start à $end, heure du chantier) : rien n’est enregistré.';
  }

  @override
  String get trackingOffDetail =>
      'La localisation est coupée ou refusée pour Polaris : activez-la dans les réglages du téléphone.';

  @override
  String get trackingBadgesDetail =>
      'Recherche des badges des ouvriers sur ce chantier tant que l’application est ouverte.';

  @override
  String trackingQueued(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count relevés en attente d’envoi.',
      one: '1 relevé en attente d’envoi.',
      zero: 'Tout est envoyé.',
    );
    return '$_temp0';
  }

  @override
  String trackingMocked(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count positions venant d’une application de fausse localisation ont été ignorées.',
      one:
          '1 position venant d’une application de fausse localisation a été ignorée.',
    );
    return '$_temp0';
  }

  @override
  String get trackingReadNotice => 'Lire la notice';

  @override
  String get trackingBackgroundTitle => 'Polaris : position activée';

  @override
  String get trackingBackgroundText =>
      'Votre position est enregistrée pendant les heures de suivi.';
}
