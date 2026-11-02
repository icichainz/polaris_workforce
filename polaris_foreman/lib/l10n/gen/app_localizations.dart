import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/intl.dart' as intl;

import 'app_localizations_en.dart';
import 'app_localizations_fr.dart';

// ignore_for_file: type=lint

/// Callers can lookup localized strings with an instance of AppLocalizations
/// returned by `AppLocalizations.of(context)`.
///
/// Applications need to include `AppLocalizations.delegate()` in their app's
/// `localizationDelegates` list, and the locales they support in the app's
/// `supportedLocales` list. For example:
///
/// ```dart
/// import 'gen/app_localizations.dart';
///
/// return MaterialApp(
///   localizationsDelegates: AppLocalizations.localizationsDelegates,
///   supportedLocales: AppLocalizations.supportedLocales,
///   home: MyApplicationHome(),
/// );
/// ```
///
/// ## Update pubspec.yaml
///
/// Please make sure to update your pubspec.yaml to include the following
/// packages:
///
/// ```yaml
/// dependencies:
///   # Internationalization support.
///   flutter_localizations:
///     sdk: flutter
///   intl: any # Use the pinned version from flutter_localizations
///
///   # Rest of dependencies
/// ```
///
/// ## iOS Applications
///
/// iOS applications define key application metadata, including supported
/// locales, in an Info.plist file that is built into the application bundle.
/// To configure the locales supported by your app, you’ll need to edit this
/// file.
///
/// First, open your project’s ios/Runner.xcworkspace Xcode workspace file.
/// Then, in the Project Navigator, open the Info.plist file under the Runner
/// project’s Runner folder.
///
/// Next, select the Information Property List item, select Add Item from the
/// Editor menu, then select Localizations from the pop-up menu.
///
/// Select and expand the newly-created Localizations item then, for each
/// locale your application supports, add a new item and select the locale
/// you wish to add from the pop-up menu in the Value field. This list should
/// be consistent with the languages listed in the AppLocalizations.supportedLocales
/// property.
abstract class AppLocalizations {
  AppLocalizations(String locale)
    : localeName = intl.Intl.canonicalizedLocale(locale.toString());

  final String localeName;

  static AppLocalizations of(BuildContext context) {
    return Localizations.of<AppLocalizations>(context, AppLocalizations)!;
  }

  static const LocalizationsDelegate<AppLocalizations> delegate =
      _AppLocalizationsDelegate();

  /// A list of this localizations delegate along with the default localizations
  /// delegates.
  ///
  /// Returns a list of localizations delegates containing this delegate along with
  /// GlobalMaterialLocalizations.delegate, GlobalCupertinoLocalizations.delegate,
  /// and GlobalWidgetsLocalizations.delegate.
  ///
  /// Additional delegates can be added by appending to this list in
  /// MaterialApp. This list does not have to be used at all if a custom list
  /// of delegates is preferred or required.
  static const List<LocalizationsDelegate<dynamic>> localizationsDelegates =
      <LocalizationsDelegate<dynamic>>[
        delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
      ];

  /// A list of this localizations delegate's supported locales.
  static const List<Locale> supportedLocales = <Locale>[
    Locale('en'),
    Locale('fr'),
  ];

  /// No description provided for @appName.
  ///
  /// In en, this message translates to:
  /// **'Polaris'**
  String get appName;

  /// No description provided for @appSub.
  ///
  /// In en, this message translates to:
  /// **'Workforce'**
  String get appSub;

  /// No description provided for @otherLanguage.
  ///
  /// In en, this message translates to:
  /// **'FR'**
  String get otherLanguage;

  /// No description provided for @switchLanguage.
  ///
  /// In en, this message translates to:
  /// **'Passer en français'**
  String get switchLanguage;

  /// No description provided for @statusPresent.
  ///
  /// In en, this message translates to:
  /// **'Present'**
  String get statusPresent;

  /// No description provided for @statusAbsent.
  ///
  /// In en, this message translates to:
  /// **'Absent'**
  String get statusAbsent;

  /// No description provided for @statusHalfDay.
  ///
  /// In en, this message translates to:
  /// **'Half day'**
  String get statusHalfDay;

  /// No description provided for @statusLate.
  ///
  /// In en, this message translates to:
  /// **'Late'**
  String get statusLate;

  /// Letter shown with the word Present.
  ///
  /// In en, this message translates to:
  /// **'P'**
  String get statusLetterPresent;

  /// Letter shown with the word Absent.
  ///
  /// In en, this message translates to:
  /// **'A'**
  String get statusLetterAbsent;

  /// Letter shown with the words Half day.
  ///
  /// In en, this message translates to:
  /// **'½'**
  String get statusLetterHalfDay;

  /// Letter shown with the word Late. Follows the translated word (French: R for Retard).
  ///
  /// In en, this message translates to:
  /// **'L'**
  String get statusLetterLate;

  /// No description provided for @phoneTitle.
  ///
  /// In en, this message translates to:
  /// **'Sign in'**
  String get phoneTitle;

  /// No description provided for @phoneLabel.
  ///
  /// In en, this message translates to:
  /// **'Phone number'**
  String get phoneLabel;

  /// No description provided for @continueButton.
  ///
  /// In en, this message translates to:
  /// **'Continue'**
  String get continueButton;

  /// No description provided for @pinPrompt.
  ///
  /// In en, this message translates to:
  /// **'Enter your 4-digit PIN'**
  String get pinPrompt;

  /// No description provided for @pinEntered.
  ///
  /// In en, this message translates to:
  /// **'PIN entered'**
  String get pinEntered;

  /// No description provided for @pinDotsLabel.
  ///
  /// In en, this message translates to:
  /// **'{count} of 4 digits entered'**
  String pinDotsLabel(int count);

  /// No description provided for @notYou.
  ///
  /// In en, this message translates to:
  /// **'Not you?'**
  String get notYou;

  /// No description provided for @foremanAt.
  ///
  /// In en, this message translates to:
  /// **'Foreman · {site}'**
  String foremanAt(String site);

  /// No description provided for @signInButton.
  ///
  /// In en, this message translates to:
  /// **'Open today’s roll call'**
  String get signInButton;

  /// No description provided for @keyClear.
  ///
  /// In en, this message translates to:
  /// **'Clear'**
  String get keyClear;

  /// No description provided for @keyDelete.
  ///
  /// In en, this message translates to:
  /// **'Delete'**
  String get keyDelete;

  /// No description provided for @wrongPin.
  ///
  /// In en, this message translates to:
  /// **'Wrong PIN. {count, plural, =1{1 try left} other{{count} tries left}}.'**
  String wrongPin(int count);

  /// No description provided for @wrongPinNoCount.
  ///
  /// In en, this message translates to:
  /// **'Wrong phone number or PIN.'**
  String get wrongPinNoCount;

  /// No description provided for @lockedUntil.
  ///
  /// In en, this message translates to:
  /// **'Too many wrong PINs. Try again at {time}.'**
  String lockedUntil(String time);

  /// No description provided for @lockedNoTime.
  ///
  /// In en, this message translates to:
  /// **'Too many wrong PINs. Try again later.'**
  String get lockedNoTime;

  /// No description provided for @throttled.
  ///
  /// In en, this message translates to:
  /// **'Too many tries. Wait {seconds} s.'**
  String throttled(int seconds);

  /// No description provided for @signInOffline.
  ///
  /// In en, this message translates to:
  /// **'No network. Signing in needs a connection.'**
  String get signInOffline;

  /// No description provided for @signInError.
  ///
  /// In en, this message translates to:
  /// **'Could not sign in ({code}).'**
  String signInError(String code);

  /// No description provided for @sessionExpired.
  ///
  /// In en, this message translates to:
  /// **'Your session ended. Enter your PIN. Unsent work stays on this phone.'**
  String get sessionExpired;

  /// No description provided for @bannerOffline.
  ///
  /// In en, this message translates to:
  /// **'Offline'**
  String get bannerOffline;

  /// No description provided for @bannerWorksNormally.
  ///
  /// In en, this message translates to:
  /// **'works normally'**
  String get bannerWorksNormally;

  /// No description provided for @bannerOnline.
  ///
  /// In en, this message translates to:
  /// **'Online'**
  String get bannerOnline;

  /// No description provided for @bannerAllSent.
  ///
  /// In en, this message translates to:
  /// **'all sent'**
  String get bannerAllSent;

  /// No description provided for @bannerSending.
  ///
  /// In en, this message translates to:
  /// **'Sending…'**
  String get bannerSending;

  /// No description provided for @bannerChecking.
  ///
  /// In en, this message translates to:
  /// **'Checking network…'**
  String get bannerChecking;

  /// No description provided for @bannerWaiting.
  ///
  /// In en, this message translates to:
  /// **'{count} waiting'**
  String bannerWaiting(int count);

  /// No description provided for @bannerFailed.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 not sent · tap to see} other{{count} not sent · tap to see}}'**
  String bannerFailed(int count);

  /// No description provided for @morningDue.
  ///
  /// In en, this message translates to:
  /// **'Morning · due {time}'**
  String morningDue(String time);

  /// No description provided for @morning.
  ///
  /// In en, this message translates to:
  /// **'Morning'**
  String get morning;

  /// No description provided for @rollCall.
  ///
  /// In en, this message translates to:
  /// **'Roll call'**
  String get rollCall;

  /// No description provided for @crewCount.
  ///
  /// In en, this message translates to:
  /// **'{count} workers on your crew list'**
  String crewCount(int count);

  /// No description provided for @startRollCall.
  ///
  /// In en, this message translates to:
  /// **'Start roll call'**
  String get startRollCall;

  /// No description provided for @continueRollCall.
  ///
  /// In en, this message translates to:
  /// **'Continue roll call'**
  String get continueRollCall;

  /// No description provided for @viewRollCall.
  ///
  /// In en, this message translates to:
  /// **'View roll call'**
  String get viewRollCall;

  /// No description provided for @fixRollCall.
  ///
  /// In en, this message translates to:
  /// **'Fix and submit again'**
  String get fixRollCall;

  /// No description provided for @sentBack.
  ///
  /// In en, this message translates to:
  /// **'Sent back: {note}'**
  String sentBack(String note);

  /// No description provided for @dayNotStarted.
  ///
  /// In en, this message translates to:
  /// **'Not started'**
  String get dayNotStarted;

  /// No description provided for @dayDraft.
  ///
  /// In en, this message translates to:
  /// **'{marked} of {total} marked'**
  String dayDraft(int marked, int total);

  /// No description provided for @daySubmitted.
  ///
  /// In en, this message translates to:
  /// **'Submitted'**
  String get daySubmitted;

  /// No description provided for @dayApproved.
  ///
  /// In en, this message translates to:
  /// **'Approved'**
  String get dayApproved;

  /// No description provided for @dayRejected.
  ///
  /// In en, this message translates to:
  /// **'Sent back'**
  String get dayRejected;

  /// No description provided for @dayLocked.
  ///
  /// In en, this message translates to:
  /// **'Locked'**
  String get dayLocked;

  /// No description provided for @dayPaid.
  ///
  /// In en, this message translates to:
  /// **'Paid'**
  String get dayPaid;

  /// No description provided for @evening.
  ///
  /// In en, this message translates to:
  /// **'Evening'**
  String get evening;

  /// No description provided for @cashPayout.
  ///
  /// In en, this message translates to:
  /// **'Cash payout'**
  String get cashPayout;

  /// No description provided for @payoutAfterApproval.
  ///
  /// In en, this message translates to:
  /// **'After approval'**
  String get payoutAfterApproval;

  /// No description provided for @payoutOpensText.
  ///
  /// In en, this message translates to:
  /// **'Opens once the site manager approves today’s roll call.'**
  String get payoutOpensText;

  /// No description provided for @previewPayout.
  ///
  /// In en, this message translates to:
  /// **'Preview tonight’s payout'**
  String get previewPayout;

  /// No description provided for @payoutOpen.
  ///
  /// In en, this message translates to:
  /// **'Open'**
  String get payoutOpen;

  /// No description provided for @payoutOpenText.
  ///
  /// In en, this message translates to:
  /// **'{count} to pay · {amount} left'**
  String payoutOpenText(int count, String amount);

  /// No description provided for @startPayout.
  ///
  /// In en, this message translates to:
  /// **'Start payout'**
  String get startPayout;

  /// No description provided for @payoutPaidPill.
  ///
  /// In en, this message translates to:
  /// **'Everyone paid'**
  String get payoutPaidPill;

  /// No description provided for @payoutClosed.
  ///
  /// In en, this message translates to:
  /// **'Closed'**
  String get payoutClosed;

  /// No description provided for @viewPayout.
  ///
  /// In en, this message translates to:
  /// **'View payout'**
  String get viewPayout;

  /// No description provided for @thisWeek.
  ///
  /// In en, this message translates to:
  /// **'This week'**
  String get thisWeek;

  /// No description provided for @signOut.
  ///
  /// In en, this message translates to:
  /// **'Sign out'**
  String get signOut;

  /// No description provided for @chooseSite.
  ///
  /// In en, this message translates to:
  /// **'Choose a site'**
  String get chooseSite;

  /// No description provided for @signOutWaiting.
  ///
  /// In en, this message translates to:
  /// **'{count} changes are not sent yet. They stay on this phone and are sent after you sign in again.'**
  String signOutWaiting(int count);

  /// No description provided for @cancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get cancel;

  /// No description provided for @loading.
  ///
  /// In en, this message translates to:
  /// **'Loading…'**
  String get loading;

  /// No description provided for @noSite.
  ///
  /// In en, this message translates to:
  /// **'No site is assigned to you. Ask the office.'**
  String get noSite;

  /// No description provided for @rollCallSubtitle.
  ///
  /// In en, this message translates to:
  /// **'{site} · {date} · saved on phone'**
  String rollCallSubtitle(String site, String date);

  /// No description provided for @tabToMark.
  ///
  /// In en, this message translates to:
  /// **'To mark · {count}'**
  String tabToMark(int count);

  /// No description provided for @tabAll.
  ///
  /// In en, this message translates to:
  /// **'All {count}'**
  String tabAll(int count);

  /// No description provided for @allPresent.
  ///
  /// In en, this message translates to:
  /// **'All P'**
  String get allPresent;

  /// No description provided for @allPresentHint.
  ///
  /// In en, this message translates to:
  /// **'Mark everyone not marked yet as present'**
  String get allPresentHint;

  /// No description provided for @reviewSubmit.
  ///
  /// In en, this message translates to:
  /// **'Review & submit'**
  String get reviewSubmit;

  /// No description provided for @reviewNotMarked.
  ///
  /// In en, this message translates to:
  /// **'Review · {count} not marked'**
  String reviewNotMarked(int count);

  /// No description provided for @statusFor.
  ///
  /// In en, this message translates to:
  /// **'Status for {name}'**
  String statusFor(String name);

  /// No description provided for @submittedReadOnly.
  ///
  /// In en, this message translates to:
  /// **'Submitted. Changes need a reason.'**
  String get submittedReadOnly;

  /// No description provided for @lockedReadOnly.
  ///
  /// In en, this message translates to:
  /// **'Locked. No more changes.'**
  String get lockedReadOnly;

  /// No description provided for @everyoneMarked.
  ///
  /// In en, this message translates to:
  /// **'Everyone is marked.'**
  String get everyoneMarked;

  /// No description provided for @reasonTitle.
  ///
  /// In en, this message translates to:
  /// **'Why does this change?'**
  String get reasonTitle;

  /// No description provided for @reasonHint.
  ///
  /// In en, this message translates to:
  /// **'Reason (required)'**
  String get reasonHint;

  /// No description provided for @save.
  ///
  /// In en, this message translates to:
  /// **'Save'**
  String get save;

  /// No description provided for @arrival.
  ///
  /// In en, this message translates to:
  /// **'Arrival'**
  String get arrival;

  /// No description provided for @departure.
  ///
  /// In en, this message translates to:
  /// **'Departure'**
  String get departure;

  /// No description provided for @noCrew.
  ///
  /// In en, this message translates to:
  /// **'No crew list on this phone yet. Connect to the network once to download it.'**
  String get noCrew;

  /// No description provided for @reviewTitle.
  ///
  /// In en, this message translates to:
  /// **'Review & submit'**
  String get reviewTitle;

  /// No description provided for @exceptionsTitle.
  ///
  /// In en, this message translates to:
  /// **'Not full days · check these'**
  String get exceptionsTitle;

  /// No description provided for @change.
  ///
  /// In en, this message translates to:
  /// **'Change'**
  String get change;

  /// No description provided for @halfDayTimes.
  ///
  /// In en, this message translates to:
  /// **'Half day · {from}–{to}'**
  String halfDayTimes(String from, String to);

  /// No description provided for @halfDayLeaves.
  ///
  /// In en, this message translates to:
  /// **'Half day · leaves {time}'**
  String halfDayLeaves(String time);

  /// No description provided for @lateArrived.
  ///
  /// In en, this message translates to:
  /// **'Late · arrived {time}'**
  String lateArrived(String time);

  /// No description provided for @absentStreak.
  ///
  /// In en, this message translates to:
  /// **'Absent · {count} days in a row'**
  String absentStreak(int count);

  /// No description provided for @absentToday.
  ///
  /// In en, this message translates to:
  /// **'Absent today'**
  String get absentToday;

  /// No description provided for @moreAbsent.
  ///
  /// In en, this message translates to:
  /// **'+ {count} more absent'**
  String moreAbsent(int count);

  /// No description provided for @noteLabel.
  ///
  /// In en, this message translates to:
  /// **'Note for the site manager'**
  String get noteLabel;

  /// No description provided for @optional.
  ///
  /// In en, this message translates to:
  /// **'Optional'**
  String get optional;

  /// No description provided for @offlineSubmitTitle.
  ///
  /// In en, this message translates to:
  /// **'You’re offline.'**
  String get offlineSubmitTitle;

  /// No description provided for @offlineSubmitBody.
  ///
  /// In en, this message translates to:
  /// **'Submitting saves the roll call on this phone and sends it as soon as there is network. You can keep working.'**
  String get offlineSubmitBody;

  /// No description provided for @submitCount.
  ///
  /// In en, this message translates to:
  /// **'Submit {count} workers'**
  String submitCount(int count);

  /// No description provided for @submittedAt.
  ///
  /// In en, this message translates to:
  /// **'Submitted {time}.'**
  String submittedAt(String time);

  /// No description provided for @submittedWaiting.
  ///
  /// In en, this message translates to:
  /// **'Waiting to send (offline) — then {manager} approves it. Changes from now on need a reason.'**
  String submittedWaiting(String manager);

  /// No description provided for @submittedSent.
  ///
  /// In en, this message translates to:
  /// **'Sent — {manager} approves it next. Changes from now on need a reason.'**
  String submittedSent(String manager);

  /// No description provided for @theSiteManager.
  ///
  /// In en, this message translates to:
  /// **'the site manager'**
  String get theSiteManager;

  /// No description provided for @backToToday.
  ///
  /// In en, this message translates to:
  /// **'Back to today'**
  String get backToToday;

  /// No description provided for @notMarkedWarning.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 worker is not marked yet.} other{{count} workers are not marked yet.}}'**
  String notMarkedWarning(int count);

  /// No description provided for @noExceptions.
  ///
  /// In en, this message translates to:
  /// **'Everyone worked a full day.'**
  String get noExceptions;

  /// No description provided for @payoutTitle.
  ///
  /// In en, this message translates to:
  /// **'Cash payout'**
  String get payoutTitle;

  /// No description provided for @payoutSubtitleApproved.
  ///
  /// In en, this message translates to:
  /// **'{site} · {date} · approved {time}'**
  String payoutSubtitleApproved(String site, String date, String time);

  /// No description provided for @payoutSubtitlePreview.
  ///
  /// In en, this message translates to:
  /// **'{site} · {date} · preview'**
  String payoutSubtitlePreview(String site, String date);

  /// No description provided for @inHand.
  ///
  /// In en, this message translates to:
  /// **'In hand'**
  String get inHand;

  /// No description provided for @paid.
  ///
  /// In en, this message translates to:
  /// **'Paid'**
  String get paid;

  /// No description provided for @left.
  ///
  /// In en, this message translates to:
  /// **'Left'**
  String get left;

  /// No description provided for @earnedToday.
  ///
  /// In en, this message translates to:
  /// **'Earned today'**
  String get earnedToday;

  /// No description provided for @advanceRepayment.
  ///
  /// In en, this message translates to:
  /// **'Advance repayment'**
  String get advanceRepayment;

  /// No description provided for @handOver.
  ///
  /// In en, this message translates to:
  /// **'Hand over'**
  String get handOver;

  /// No description provided for @proofOfReceipt.
  ///
  /// In en, this message translates to:
  /// **'Proof of receipt'**
  String get proofOfReceipt;

  /// No description provided for @proofPhoto.
  ///
  /// In en, this message translates to:
  /// **'Photo'**
  String get proofPhoto;

  /// No description provided for @proofSignature.
  ///
  /// In en, this message translates to:
  /// **'Signature'**
  String get proofSignature;

  /// No description provided for @proofThumb.
  ///
  /// In en, this message translates to:
  /// **'Thumb mark'**
  String get proofThumb;

  /// Proof button: pay without a photo, signature or thumb mark (only when the company accepts it).
  ///
  /// In en, this message translates to:
  /// **'No proof'**
  String get proofNone;

  /// No description provided for @confirmPaid.
  ///
  /// In en, this message translates to:
  /// **'Confirm paid · {amount}'**
  String confirmPaid(String amount);

  /// No description provided for @everyonePaid.
  ///
  /// In en, this message translates to:
  /// **'Everyone is paid'**
  String get everyonePaid;

  /// No description provided for @returnCash.
  ///
  /// In en, this message translates to:
  /// **'Return {amount} to the office. The sheet locks when the office counts it.'**
  String returnCash(String amount);

  /// No description provided for @queue.
  ///
  /// In en, this message translates to:
  /// **'Queue'**
  String get queue;

  /// No description provided for @queuePaid.
  ///
  /// In en, this message translates to:
  /// **'Paid'**
  String get queuePaid;

  /// No description provided for @queueNow.
  ///
  /// In en, this message translates to:
  /// **'Now'**
  String get queueNow;

  /// No description provided for @queueToPay.
  ///
  /// In en, this message translates to:
  /// **'To pay'**
  String get queueToPay;

  /// No description provided for @previewBanner.
  ///
  /// In en, this message translates to:
  /// **'Preview. The payout opens when {manager} approves the roll call.'**
  String previewBanner(String manager);

  /// No description provided for @proofReady.
  ///
  /// In en, this message translates to:
  /// **'Proof ready'**
  String get proofReady;

  /// No description provided for @redoProof.
  ///
  /// In en, this message translates to:
  /// **'Redo'**
  String get redoProof;

  /// No description provided for @takeProofFirst.
  ///
  /// In en, this message translates to:
  /// **'Take the proof first'**
  String get takeProofFirst;

  /// No description provided for @signHere.
  ///
  /// In en, this message translates to:
  /// **'Ask the worker to sign'**
  String get signHere;

  /// No description provided for @thumbHere.
  ///
  /// In en, this message translates to:
  /// **'Ask the worker to press a thumb on the screen'**
  String get thumbHere;

  /// No description provided for @clear.
  ///
  /// In en, this message translates to:
  /// **'Clear'**
  String get clear;

  /// No description provided for @done.
  ///
  /// In en, this message translates to:
  /// **'Done'**
  String get done;

  /// No description provided for @notSentYet.
  ///
  /// In en, this message translates to:
  /// **'not sent yet'**
  String get notSentYet;

  /// No description provided for @noPayout.
  ///
  /// In en, this message translates to:
  /// **'No payout sheet on this phone yet. Connect to the network to load it.'**
  String get noPayout;

  /// No description provided for @photoFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not take the photo.'**
  String get photoFailed;

  /// No description provided for @payoutClosedText.
  ///
  /// In en, this message translates to:
  /// **'The office closed this sheet.'**
  String get payoutClosedText;

  /// No description provided for @failedTitle.
  ///
  /// In en, this message translates to:
  /// **'Not sent'**
  String get failedTitle;

  /// No description provided for @failedHint.
  ///
  /// In en, this message translates to:
  /// **'The server refused these changes. Check with the office.'**
  String get failedHint;

  /// No description provided for @discard.
  ///
  /// In en, this message translates to:
  /// **'Discard'**
  String get discard;

  /// No description provided for @kindRecords.
  ///
  /// In en, this message translates to:
  /// **'Roll call marks · {date}'**
  String kindRecords(String date);

  /// No description provided for @kindSubmit.
  ///
  /// In en, this message translates to:
  /// **'Roll call submit · {date}'**
  String kindSubmit(String date);

  /// No description provided for @kindCorrection.
  ///
  /// In en, this message translates to:
  /// **'Correction · {date}'**
  String kindCorrection(String date);

  /// No description provided for @kindPayment.
  ///
  /// In en, this message translates to:
  /// **'Payment · {date}'**
  String kindPayment(String date);

  /// No description provided for @mockOfflineOn.
  ///
  /// In en, this message translates to:
  /// **'Mock: offline'**
  String get mockOfflineOn;

  /// No description provided for @mockOfflineOff.
  ///
  /// In en, this message translates to:
  /// **'Mock: online'**
  String get mockOfflineOff;

  /// No description provided for @mockKioskOn.
  ///
  /// In en, this message translates to:
  /// **'Demo: this phone is now the kiosk of {site}. Long-press the site name to end it.'**
  String mockKioskOn(String site);

  /// No description provided for @mockKioskOff.
  ///
  /// In en, this message translates to:
  /// **'Demo: kiosk mode ended.'**
  String get mockKioskOff;

  /// No description provided for @kindCheckIn.
  ///
  /// In en, this message translates to:
  /// **'Check-in · {date}'**
  String kindCheckIn(String date);

  /// No description provided for @scanBadges.
  ///
  /// In en, this message translates to:
  /// **'Scan badges'**
  String get scanBadges;

  /// No description provided for @scanBadgesCount.
  ///
  /// In en, this message translates to:
  /// **'Scan badges · {count} today'**
  String scanBadgesCount(int count);

  /// No description provided for @scanTitle.
  ///
  /// In en, this message translates to:
  /// **'Scan badges'**
  String get scanTitle;

  /// No description provided for @scanCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =0{No scan} =1{1 scan} other{{count} scans}}'**
  String scanCount(int count);

  /// No description provided for @directionIn.
  ///
  /// In en, this message translates to:
  /// **'In'**
  String get directionIn;

  /// No description provided for @directionOut.
  ///
  /// In en, this message translates to:
  /// **'Out'**
  String get directionOut;

  /// No description provided for @photoWithScan.
  ///
  /// In en, this message translates to:
  /// **'Take a photo with each scan'**
  String get photoWithScan;

  /// No description provided for @todayScans.
  ///
  /// In en, this message translates to:
  /// **'Today’s scans'**
  String get todayScans;

  /// No description provided for @backToRollCall.
  ///
  /// In en, this message translates to:
  /// **'Back to the roll call'**
  String get backToRollCall;

  /// No description provided for @simulateScan.
  ///
  /// In en, this message translates to:
  /// **'Simulate a scan (mock)'**
  String get simulateScan;

  /// No description provided for @scanQueued.
  ///
  /// In en, this message translates to:
  /// **'Saved on the phone · not sent yet'**
  String get scanQueued;

  /// No description provided for @scanSent.
  ///
  /// In en, this message translates to:
  /// **'Sent'**
  String get scanSent;

  /// No description provided for @badgeNotRecognisedTitle.
  ///
  /// In en, this message translates to:
  /// **'Badge not recognised'**
  String get badgeNotRecognisedTitle;

  /// No description provided for @badgeNotRecognised.
  ///
  /// In en, this message translates to:
  /// **'Badge not recognised — see the foreman'**
  String get badgeNotRecognised;

  /// No description provided for @badgeRevoked.
  ///
  /// In en, this message translates to:
  /// **'This badge is no longer valid'**
  String get badgeRevoked;

  /// No description provided for @notOnCrew.
  ///
  /// In en, this message translates to:
  /// **'Not on today’s crew — tell the foreman'**
  String get notOnCrew;

  /// No description provided for @outsideSite.
  ///
  /// In en, this message translates to:
  /// **'Outside the site'**
  String get outsideSite;

  /// No description provided for @alreadyCheckedIn.
  ///
  /// In en, this message translates to:
  /// **'Already checked in'**
  String get alreadyCheckedIn;

  /// No description provided for @alreadyCheckedOut.
  ///
  /// In en, this message translates to:
  /// **'Already checked out'**
  String get alreadyCheckedOut;

  /// No description provided for @workerNumber.
  ///
  /// In en, this message translates to:
  /// **'Worker #{id}'**
  String workerNumber(int id);

  /// No description provided for @gpsReady.
  ///
  /// In en, this message translates to:
  /// **'GPS on · ±{metres} m'**
  String gpsReady(int metres);

  /// No description provided for @gpsReadyNoAccuracy.
  ///
  /// In en, this message translates to:
  /// **'GPS on'**
  String get gpsReadyNoAccuracy;

  /// No description provided for @gpsLooking.
  ///
  /// In en, this message translates to:
  /// **'Looking for GPS…'**
  String get gpsLooking;

  /// No description provided for @gpsDenied.
  ///
  /// In en, this message translates to:
  /// **'No GPS permission: scans are sent without position'**
  String get gpsDenied;

  /// No description provided for @gpsOff.
  ///
  /// In en, this message translates to:
  /// **'Location is off on this phone: scans are sent without position'**
  String get gpsOff;

  /// No description provided for @cameraDenied.
  ///
  /// In en, this message translates to:
  /// **'Allow the camera in the phone’s settings to scan badges.'**
  String get cameraDenied;

  /// No description provided for @cameraError.
  ///
  /// In en, this message translates to:
  /// **'The camera cannot start.'**
  String get cameraError;

  /// No description provided for @selfCheckInNote.
  ///
  /// In en, this message translates to:
  /// **'Workers check in themselves: the roll call shows what their scans wrote. Check it and submit.'**
  String get selfCheckInNote;

  /// No description provided for @prefilledNote.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 mark comes from badge scans. Check it.} other{{count} marks come from badge scans. Check them.}}'**
  String prefilledNote(int count);

  /// No description provided for @fromCheckIn.
  ///
  /// In en, this message translates to:
  /// **'From badge scan'**
  String get fromCheckIn;

  /// No description provided for @kioskTitle.
  ///
  /// In en, this message translates to:
  /// **'Check-in kiosk'**
  String get kioskTitle;

  /// No description provided for @kioskHint.
  ///
  /// In en, this message translates to:
  /// **'Show your badge'**
  String get kioskHint;

  /// No description provided for @switchCamera.
  ///
  /// In en, this message translates to:
  /// **'Switch camera'**
  String get switchCamera;

  /// No description provided for @kioskExit.
  ///
  /// In en, this message translates to:
  /// **'Leave the kiosk'**
  String get kioskExit;

  /// No description provided for @kioskExitText.
  ///
  /// In en, this message translates to:
  /// **'This phone stays a kiosk until the office ends kiosk mode. Sign out?'**
  String get kioskExitText;

  /// No description provided for @kioskPinTitle.
  ///
  /// In en, this message translates to:
  /// **'Foreman PIN'**
  String get kioskPinTitle;

  /// No description provided for @kioskPinWrong.
  ///
  /// In en, this message translates to:
  /// **'Wrong PIN'**
  String get kioskPinWrong;

  /// No description provided for @kindConsent.
  ///
  /// In en, this message translates to:
  /// **'Tracking answer · {date}'**
  String kindConsent(String date);

  /// No description provided for @trackingConsentTitle.
  ///
  /// In en, this message translates to:
  /// **'Location tracking'**
  String get trackingConsentTitle;

  /// No description provided for @trackingConsentIntro.
  ///
  /// In en, this message translates to:
  /// **'Your company uses Polaris to know who is on its sites. Read how it works, then choose.'**
  String get trackingConsentIntro;

  /// No description provided for @trackingConsentWhenTitle.
  ///
  /// In en, this message translates to:
  /// **'When'**
  String get trackingConsentWhenTitle;

  /// No description provided for @trackingConsentWhenHours.
  ///
  /// In en, this message translates to:
  /// **'Only during tracking hours, {start}–{end} (site time), and only while you are signed in on this phone. Outside those hours nothing is recorded.'**
  String trackingConsentWhenHours(String start, String end);

  /// No description provided for @trackingConsentWhenGeneric.
  ///
  /// In en, this message translates to:
  /// **'Only during the tracking hours set by your company, and only while you are signed in on this phone. Outside those hours nothing is recorded.'**
  String get trackingConsentWhenGeneric;

  /// No description provided for @trackingConsentWhatTitle.
  ///
  /// In en, this message translates to:
  /// **'What is recorded'**
  String get trackingConsentWhatTitle;

  /// No description provided for @trackingConsentWhatText.
  ///
  /// In en, this message translates to:
  /// **'The phone’s position {every}, with its accuracy, and the workers’ badges the phone picks up nearby while the app is open on a site. Never your calls, messages, photos or other apps.'**
  String trackingConsentWhatText(String every);

  /// No description provided for @trackingEveryMinutes.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{about every minute} other{about every {count} minutes}}'**
  String trackingEveryMinutes(int count);

  /// No description provided for @trackingEverySeconds.
  ///
  /// In en, this message translates to:
  /// **'about every {count} seconds'**
  String trackingEverySeconds(int count);

  /// No description provided for @trackingEveryGeneric.
  ///
  /// In en, this message translates to:
  /// **'at regular intervals'**
  String get trackingEveryGeneric;

  /// No description provided for @trackingConsentKeepTitle.
  ///
  /// In en, this message translates to:
  /// **'How long it is kept'**
  String get trackingConsentKeepTitle;

  /// No description provided for @trackingConsentKeepDays.
  ///
  /// In en, this message translates to:
  /// **'Raw positions are deleted after {days} days. Arrival and departure times are kept like attendance.'**
  String trackingConsentKeepDays(int days);

  /// No description provided for @trackingConsentKeepGeneric.
  ///
  /// In en, this message translates to:
  /// **'Raw positions are deleted after the period set by your company (30 days unless it changed it). Arrival and departure times are kept like attendance.'**
  String get trackingConsentKeepGeneric;

  /// No description provided for @trackingConsentWhoTitle.
  ///
  /// In en, this message translates to:
  /// **'Who sees it'**
  String get trackingConsentWhoTitle;

  /// No description provided for @trackingConsentWhoText.
  ///
  /// In en, this message translates to:
  /// **'Site managers see who is on their own sites. Trips between sites are visible to company administrators only. You can ask the office for your own history.'**
  String get trackingConsentWhoText;

  /// No description provided for @trackingConsentUseTitle.
  ///
  /// In en, this message translates to:
  /// **'What it is used for'**
  String get trackingConsentUseTitle;

  /// No description provided for @trackingConsentUseText.
  ///
  /// In en, this message translates to:
  /// **'Presence only suggests attendance: the roll call is still confirmed by a person. It is not used to discipline anyone automatically.'**
  String get trackingConsentUseText;

  /// No description provided for @trackingConsentChoice.
  ///
  /// In en, this message translates to:
  /// **'You can refuse: the app works the same, without tracking. You can change your answer later.'**
  String get trackingConsentChoice;

  /// No description provided for @trackingConsentAccept.
  ///
  /// In en, this message translates to:
  /// **'Accept'**
  String get trackingConsentAccept;

  /// No description provided for @trackingConsentRefuse.
  ///
  /// In en, this message translates to:
  /// **'Refuse'**
  String get trackingConsentRefuse;

  /// No description provided for @trackingConsentVersion.
  ///
  /// In en, this message translates to:
  /// **'Notice version {version}'**
  String trackingConsentVersion(String version);

  /// No description provided for @trackingConsentSaved.
  ///
  /// In en, this message translates to:
  /// **'Answer saved. It is sent as soon as the phone is online.'**
  String get trackingConsentSaved;

  /// No description provided for @trackingConsentNeeded.
  ///
  /// In en, this message translates to:
  /// **'Location tracking: read the notice'**
  String get trackingConsentNeeded;

  /// No description provided for @trackingMenu.
  ///
  /// In en, this message translates to:
  /// **'Location tracking'**
  String get trackingMenu;

  /// No description provided for @trackingOn.
  ///
  /// In en, this message translates to:
  /// **'Location on'**
  String get trackingOn;

  /// No description provided for @trackingPaused.
  ///
  /// In en, this message translates to:
  /// **'Location paused'**
  String get trackingPaused;

  /// No description provided for @trackingOff.
  ///
  /// In en, this message translates to:
  /// **'Location off'**
  String get trackingOff;

  /// No description provided for @trackingOnDetail.
  ///
  /// In en, this message translates to:
  /// **'Your position is recorded during tracking hours, {start}–{end} (site time).'**
  String trackingOnDetail(String start, String end);

  /// No description provided for @trackingPausedDetail.
  ///
  /// In en, this message translates to:
  /// **'Outside tracking hours ({start}–{end}, site time): nothing is recorded.'**
  String trackingPausedDetail(String start, String end);

  /// No description provided for @trackingOffDetail.
  ///
  /// In en, this message translates to:
  /// **'Location is off or not allowed for Polaris: turn it on in the phone’s settings.'**
  String get trackingOffDetail;

  /// No description provided for @trackingBadgesDetail.
  ///
  /// In en, this message translates to:
  /// **'Looking for workers’ badges on this site while the app is open.'**
  String get trackingBadgesDetail;

  /// No description provided for @trackingQueued.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =0{Everything is sent.} =1{1 record waiting to be sent.} other{{count} records waiting to be sent.}}'**
  String trackingQueued(int count);

  /// No description provided for @trackingMocked.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 position from a fake-location app was ignored.} other{{count} positions from a fake-location app were ignored.}}'**
  String trackingMocked(int count);

  /// No description provided for @trackingReadNotice.
  ///
  /// In en, this message translates to:
  /// **'Read the notice'**
  String get trackingReadNotice;

  /// No description provided for @trackingBackgroundTitle.
  ///
  /// In en, this message translates to:
  /// **'Polaris: location on'**
  String get trackingBackgroundTitle;

  /// No description provided for @trackingBackgroundText.
  ///
  /// In en, this message translates to:
  /// **'Your position is recorded during tracking hours.'**
  String get trackingBackgroundText;

  /// Presence tracking saw the worker on site from … to … (HH:MM).
  ///
  /// In en, this message translates to:
  /// **'Seen {start}–{end}'**
  String presenceSeen(String start, String end);

  /// No description provided for @presenceSeenOpen.
  ///
  /// In en, this message translates to:
  /// **'Seen since {start}, still on site'**
  String presenceSeenOpen(String start);

  /// No description provided for @presenceZones.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{zone {zones}} other{zones {zones}}}'**
  String presenceZones(int count, String zones);

  /// Button: mark the worker as presence tracking suggests.
  ///
  /// In en, this message translates to:
  /// **'Accept: {status}'**
  String acceptSuggestion(String status);

  /// No description provided for @acceptSuggestionHint.
  ///
  /// In en, this message translates to:
  /// **'Mark {name} as presence tracking suggests'**
  String acceptSuggestionHint(String name);

  /// No description provided for @presenceSuggestedNote.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 worker was seen on site but not marked. Check and accept.} other{{count} workers were seen on site but not marked. Check and accept.}}'**
  String presenceSuggestedNote(int count);
}

class _AppLocalizationsDelegate
    extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();

  @override
  Future<AppLocalizations> load(Locale locale) {
    return SynchronousFuture<AppLocalizations>(lookupAppLocalizations(locale));
  }

  @override
  bool isSupported(Locale locale) =>
      <String>['en', 'fr'].contains(locale.languageCode);

  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}

AppLocalizations lookupAppLocalizations(Locale locale) {
  // Lookup logic when only language code is specified.
  switch (locale.languageCode) {
    case 'en':
      return AppLocalizationsEn();
    case 'fr':
      return AppLocalizationsFr();
  }

  throw FlutterError(
    'AppLocalizations.delegate failed to load unsupported locale "$locale". This is likely '
    'an issue with the localizations generation tool. Please file an issue '
    'on GitHub with a reproducible sample app and the gen-l10n configuration '
    'that was used.',
  );
}
