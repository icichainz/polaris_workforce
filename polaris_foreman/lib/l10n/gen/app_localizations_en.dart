// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for English (`en`).
class AppLocalizationsEn extends AppLocalizations {
  AppLocalizationsEn([String locale = 'en']) : super(locale);

  @override
  String get appName => 'Polaris';

  @override
  String get appSub => 'Workforce';

  @override
  String get otherLanguage => 'FR';

  @override
  String get switchLanguage => 'Passer en français';

  @override
  String get statusPresent => 'Present';

  @override
  String get statusAbsent => 'Absent';

  @override
  String get statusHalfDay => 'Half day';

  @override
  String get statusLate => 'Late';

  @override
  String get statusLetterPresent => 'P';

  @override
  String get statusLetterAbsent => 'A';

  @override
  String get statusLetterHalfDay => '½';

  @override
  String get statusLetterLate => 'L';

  @override
  String get phoneTitle => 'Sign in';

  @override
  String get phoneLabel => 'Phone number';

  @override
  String get continueButton => 'Continue';

  @override
  String get pinPrompt => 'Enter your 4-digit PIN';

  @override
  String get pinEntered => 'PIN entered';

  @override
  String pinDotsLabel(int count) {
    return '$count of 4 digits entered';
  }

  @override
  String get notYou => 'Not you?';

  @override
  String foremanAt(String site) {
    return 'Foreman · $site';
  }

  @override
  String get signInButton => 'Open today’s roll call';

  @override
  String get keyClear => 'Clear';

  @override
  String get keyDelete => 'Delete';

  @override
  String wrongPin(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count tries left',
      one: '1 try left',
    );
    return 'Wrong PIN. $_temp0.';
  }

  @override
  String get wrongPinNoCount => 'Wrong phone number or PIN.';

  @override
  String lockedUntil(String time) {
    return 'Too many wrong PINs. Try again at $time.';
  }

  @override
  String get lockedNoTime => 'Too many wrong PINs. Try again later.';

  @override
  String throttled(int seconds) {
    return 'Too many tries. Wait $seconds s.';
  }

  @override
  String get signInOffline => 'No network. Signing in needs a connection.';

  @override
  String signInError(String code) {
    return 'Could not sign in ($code).';
  }

  @override
  String get sessionExpired =>
      'Your session ended. Enter your PIN. Unsent work stays on this phone.';

  @override
  String get bannerOffline => 'Offline';

  @override
  String get bannerWorksNormally => 'works normally';

  @override
  String get bannerOnline => 'Online';

  @override
  String get bannerAllSent => 'all sent';

  @override
  String get bannerSending => 'Sending…';

  @override
  String get bannerChecking => 'Checking network…';

  @override
  String bannerWaiting(int count) {
    return '$count waiting';
  }

  @override
  String bannerFailed(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count not sent · tap to see',
      one: '1 not sent · tap to see',
    );
    return '$_temp0';
  }

  @override
  String morningDue(String time) {
    return 'Morning · due $time';
  }

  @override
  String get morning => 'Morning';

  @override
  String get rollCall => 'Roll call';

  @override
  String crewCount(int count) {
    return '$count workers on your crew list';
  }

  @override
  String get startRollCall => 'Start roll call';

  @override
  String get continueRollCall => 'Continue roll call';

  @override
  String get viewRollCall => 'View roll call';

  @override
  String get fixRollCall => 'Fix and submit again';

  @override
  String sentBack(String note) {
    return 'Sent back: $note';
  }

  @override
  String get dayNotStarted => 'Not started';

  @override
  String dayDraft(int marked, int total) {
    return '$marked of $total marked';
  }

  @override
  String get daySubmitted => 'Submitted';

  @override
  String get dayApproved => 'Approved';

  @override
  String get dayRejected => 'Sent back';

  @override
  String get dayLocked => 'Locked';

  @override
  String get dayPaid => 'Paid';

  @override
  String get evening => 'Evening';

  @override
  String get cashPayout => 'Cash payout';

  @override
  String get payoutAfterApproval => 'After approval';

  @override
  String get payoutOpensText =>
      'Opens once the site manager approves today’s roll call.';

  @override
  String get previewPayout => 'Preview tonight’s payout';

  @override
  String get payoutOpen => 'Open';

  @override
  String payoutOpenText(int count, String amount) {
    return '$count to pay · $amount left';
  }

  @override
  String get startPayout => 'Start payout';

  @override
  String get payoutPaidPill => 'Everyone paid';

  @override
  String get payoutClosed => 'Closed';

  @override
  String get viewPayout => 'View payout';

  @override
  String get thisWeek => 'This week';

  @override
  String get signOut => 'Sign out';

  @override
  String get chooseSite => 'Choose a site';

  @override
  String signOutWaiting(int count) {
    return '$count changes are not sent yet. They stay on this phone and are sent after you sign in again.';
  }

  @override
  String get cancel => 'Cancel';

  @override
  String get loading => 'Loading…';

  @override
  String get noSite => 'No site is assigned to you. Ask the office.';

  @override
  String rollCallSubtitle(String site, String date) {
    return '$site · $date · saved on phone';
  }

  @override
  String tabToMark(int count) {
    return 'To mark · $count';
  }

  @override
  String tabAll(int count) {
    return 'All $count';
  }

  @override
  String get allPresent => 'All P';

  @override
  String get allPresentHint => 'Mark everyone not marked yet as present';

  @override
  String get reviewSubmit => 'Review & submit';

  @override
  String reviewNotMarked(int count) {
    return 'Review · $count not marked';
  }

  @override
  String statusFor(String name) {
    return 'Status for $name';
  }

  @override
  String get submittedReadOnly => 'Submitted. Changes need a reason.';

  @override
  String get lockedReadOnly => 'Locked. No more changes.';

  @override
  String get everyoneMarked => 'Everyone is marked.';

  @override
  String get reasonTitle => 'Why does this change?';

  @override
  String get reasonHint => 'Reason (required)';

  @override
  String get save => 'Save';

  @override
  String get arrival => 'Arrival';

  @override
  String get departure => 'Departure';

  @override
  String get noCrew =>
      'No crew list on this phone yet. Connect to the network once to download it.';

  @override
  String get reviewTitle => 'Review & submit';

  @override
  String get exceptionsTitle => 'Not full days · check these';

  @override
  String get change => 'Change';

  @override
  String halfDayTimes(String from, String to) {
    return 'Half day · $from–$to';
  }

  @override
  String halfDayLeaves(String time) {
    return 'Half day · leaves $time';
  }

  @override
  String lateArrived(String time) {
    return 'Late · arrived $time';
  }

  @override
  String absentStreak(int count) {
    return 'Absent · $count days in a row';
  }

  @override
  String get absentToday => 'Absent today';

  @override
  String moreAbsent(int count) {
    return '+ $count more absent';
  }

  @override
  String get noteLabel => 'Note for the site manager';

  @override
  String get optional => 'Optional';

  @override
  String get offlineSubmitTitle => 'You’re offline.';

  @override
  String get offlineSubmitBody =>
      'Submitting saves the roll call on this phone and sends it as soon as there is network. You can keep working.';

  @override
  String submitCount(int count) {
    return 'Submit $count workers';
  }

  @override
  String submittedAt(String time) {
    return 'Submitted $time.';
  }

  @override
  String submittedWaiting(String manager) {
    return 'Waiting to send (offline) — then $manager approves it. Changes from now on need a reason.';
  }

  @override
  String submittedSent(String manager) {
    return 'Sent — $manager approves it next. Changes from now on need a reason.';
  }

  @override
  String get theSiteManager => 'the site manager';

  @override
  String get backToToday => 'Back to today';

  @override
  String notMarkedWarning(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count workers are not marked yet.',
      one: '1 worker is not marked yet.',
    );
    return '$_temp0';
  }

  @override
  String get noExceptions => 'Everyone worked a full day.';

  @override
  String get payoutTitle => 'Cash payout';

  @override
  String payoutSubtitleApproved(String site, String date, String time) {
    return '$site · $date · approved $time';
  }

  @override
  String payoutSubtitlePreview(String site, String date) {
    return '$site · $date · preview';
  }

  @override
  String get inHand => 'In hand';

  @override
  String get paid => 'Paid';

  @override
  String get left => 'Left';

  @override
  String get earnedToday => 'Earned today';

  @override
  String get advanceRepayment => 'Advance repayment';

  @override
  String get handOver => 'Hand over';

  @override
  String get proofOfReceipt => 'Proof of receipt';

  @override
  String get proofPhoto => 'Photo';

  @override
  String get proofSignature => 'Signature';

  @override
  String get proofThumb => 'Thumb mark';

  @override
  String get proofNone => 'No proof';

  @override
  String confirmPaid(String amount) {
    return 'Confirm paid · $amount';
  }

  @override
  String get everyonePaid => 'Everyone is paid';

  @override
  String returnCash(String amount) {
    return 'Return $amount to the office. The sheet locks when the office counts it.';
  }

  @override
  String get queue => 'Queue';

  @override
  String get queuePaid => 'Paid';

  @override
  String get queueNow => 'Now';

  @override
  String get queueToPay => 'To pay';

  @override
  String previewBanner(String manager) {
    return 'Preview. The payout opens when $manager approves the roll call.';
  }

  @override
  String get proofReady => 'Proof ready';

  @override
  String get redoProof => 'Redo';

  @override
  String get takeProofFirst => 'Take the proof first';

  @override
  String get signHere => 'Ask the worker to sign';

  @override
  String get thumbHere => 'Ask the worker to press a thumb on the screen';

  @override
  String get clear => 'Clear';

  @override
  String get done => 'Done';

  @override
  String get notSentYet => 'not sent yet';

  @override
  String get noPayout =>
      'No payout sheet on this phone yet. Connect to the network to load it.';

  @override
  String get photoFailed => 'Could not take the photo.';

  @override
  String get payoutClosedText => 'The office closed this sheet.';

  @override
  String get failedTitle => 'Not sent';

  @override
  String get failedHint =>
      'The server refused these changes. Check with the office.';

  @override
  String get discard => 'Discard';

  @override
  String kindRecords(String date) {
    return 'Roll call marks · $date';
  }

  @override
  String kindSubmit(String date) {
    return 'Roll call submit · $date';
  }

  @override
  String kindCorrection(String date) {
    return 'Correction · $date';
  }

  @override
  String kindPayment(String date) {
    return 'Payment · $date';
  }

  @override
  String get mockOfflineOn => 'Mock: offline';

  @override
  String get mockOfflineOff => 'Mock: online';

  @override
  String mockKioskOn(String site) {
    return 'Demo: this phone is now the kiosk of $site. Long-press the site name to end it.';
  }

  @override
  String get mockKioskOff => 'Demo: kiosk mode ended.';

  @override
  String kindCheckIn(String date) {
    return 'Check-in · $date';
  }

  @override
  String get scanBadges => 'Scan badges';

  @override
  String scanBadgesCount(int count) {
    return 'Scan badges · $count today';
  }

  @override
  String get scanTitle => 'Scan badges';

  @override
  String scanCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count scans',
      one: '1 scan',
      zero: 'No scan',
    );
    return '$_temp0';
  }

  @override
  String get directionIn => 'In';

  @override
  String get directionOut => 'Out';

  @override
  String get photoWithScan => 'Take a photo with each scan';

  @override
  String get todayScans => 'Today’s scans';

  @override
  String get backToRollCall => 'Back to the roll call';

  @override
  String get simulateScan => 'Simulate a scan (mock)';

  @override
  String get scanQueued => 'Saved on the phone · not sent yet';

  @override
  String get scanSent => 'Sent';

  @override
  String get badgeNotRecognisedTitle => 'Badge not recognised';

  @override
  String get badgeNotRecognised => 'Badge not recognised — see the foreman';

  @override
  String get badgeRevoked => 'This badge is no longer valid';

  @override
  String get notOnCrew => 'Not on today’s crew — tell the foreman';

  @override
  String get outsideSite => 'Outside the site';

  @override
  String get alreadyCheckedIn => 'Already checked in';

  @override
  String get alreadyCheckedOut => 'Already checked out';

  @override
  String workerNumber(int id) {
    return 'Worker #$id';
  }

  @override
  String gpsReady(int metres) {
    return 'GPS on · ±$metres m';
  }

  @override
  String get gpsReadyNoAccuracy => 'GPS on';

  @override
  String get gpsLooking => 'Looking for GPS…';

  @override
  String get gpsDenied => 'No GPS permission: scans are sent without position';

  @override
  String get gpsOff =>
      'Location is off on this phone: scans are sent without position';

  @override
  String get cameraDenied =>
      'Allow the camera in the phone’s settings to scan badges.';

  @override
  String get cameraError => 'The camera cannot start.';

  @override
  String get selfCheckInNote =>
      'Workers check in themselves: the roll call shows what their scans wrote. Check it and submit.';

  @override
  String prefilledNote(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count marks come from badge scans. Check them.',
      one: '1 mark comes from badge scans. Check it.',
    );
    return '$_temp0';
  }

  @override
  String get fromCheckIn => 'From badge scan';

  @override
  String get kioskTitle => 'Check-in kiosk';

  @override
  String get kioskHint => 'Show your badge';

  @override
  String get switchCamera => 'Switch camera';

  @override
  String get kioskExit => 'Leave the kiosk';

  @override
  String get kioskExitText =>
      'This phone stays a kiosk until the office ends kiosk mode. Sign out?';

  @override
  String get kioskPinTitle => 'Foreman PIN';

  @override
  String get kioskPinWrong => 'Wrong PIN';
}
