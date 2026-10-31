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
