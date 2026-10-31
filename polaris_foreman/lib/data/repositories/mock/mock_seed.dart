import '../../models/models.dart';

/// Sample data from the design artboards: Riverside Apartments, 42 workers,
/// Kossi Amegah as foreman, rates 3 500–8 000 XOF a day.
class SeedWorker {
  const SeedWorker(
    this.id,
    this.firstName,
    this.lastName,
    this.trade,
    this.rate, {
    this.payType = PayType.daily,
    this.advance = 0,
    this.absentStreak = 0,
  });

  final int id;
  final String firstName;
  final String lastName;
  final String trade;

  /// Daily rate (or hourly rate for hourly workers), in XOF.
  final int rate;
  final PayType payType;

  /// Advance repayment taken from each pay.
  final int advance;
  final int absentStreak;
}

abstract final class MockSeed {
  static const foremanPhone = '+22890112233';
  static const foremanPin = '1234';
  static const siteId = 3;

  static const manager = PersonRef(id: 4, name: 'Edem Kpodar');
  static const foreman = PersonRef(id: 7, name: 'Kossi Amegah');

  static const site = AssignedSite(
    id: siteId,
    name: 'Riverside Apartments',
    address: 'Boulevard du Mono, Lomé',
    manager: manager,
    workStart: '07:00',
    rollCallDue: '08:00',
  );

  static const site2 = AssignedSite(
    id: 5,
    name: 'Agoè Warehouse',
    address: 'Route de Kpalimé, Agoè',
    manager: PersonRef(id: 9, name: 'Ama Lawson'),
    workStart: '07:30',
    rollCallDue: '08:30',
  );

  static const company = Company(
    country: 'TG',
    timeZone: 'Africa/Lome',
    currency: Currency.xof,
  );

  static Me me({Language language = Language.fr}) => Me(
    id: foreman.id,
    name: foreman.name,
    role: UserRole.foreman,
    phone: foremanPhone,
    language: language,
    company: company,
    sites: const [site, site2],
  );

  static const workers = <SeedWorker>[
    SeedWorker(44, 'Yao', 'Mensah', 'Mason', 6000, advance: 2000),
    SeedWorker(51, 'Komlan', 'Agbeko', 'Carpenter', 5500),
    SeedWorker(52, 'Afi', 'Dosseh', 'Labourer', 3500),
    SeedWorker(58, 'Kodjo', 'Atsu', 'Labourer', 3500, absentStreak: 2),
    SeedWorker(59, 'Akouvi', 'Tete', 'Steel fixer', 5000, advance: 2000),
    SeedWorker(60, 'Sena', 'Kumah', 'Mason', 6000),
    SeedWorker(63, 'Delali', 'Agbo', 'Painter', 4500),
    SeedWorker(64, 'Selom', 'Akakpo', 'Welder', 7500, absentStreak: 1),
    SeedWorker(65, 'Edem', 'Agbodjan', 'Labourer', 3500),
    SeedWorker(66, 'Yawa', 'Sossou', 'Labourer', 3500),
    SeedWorker(67, 'Dela', 'Kumassi', 'Plumber', 6500),
    SeedWorker(68, 'Kofi', 'Adjovi', 'Electrician', 8000, advance: 3000),
    SeedWorker(69, 'Ablavi', 'Amouzou', 'Labourer', 3500),
    SeedWorker(70, 'Mawuli', 'Ahadji', 'Carpenter', 5500),
    SeedWorker(71, 'Esi', 'Akpene', 'Labourer', 3500),
    SeedWorker(72, 'Koffi', 'Gbeasor', 'Mason', 6000),
    SeedWorker(73, 'Kokou', 'Adzra', 'Steel fixer', 5000),
    SeedWorker(74, 'Enyonam', 'Dzidzienyo', 'Painter', 4500),
    SeedWorker(75, 'Kossivi', 'Agbenyega', 'Welder', 7500),
    SeedWorker(76, 'Ayele', 'Fiagbe', 'Labourer', 3500, advance: 1000),
    SeedWorker(77, 'Messan', 'Hounkpati', 'Mason', 6000),
    SeedWorker(78, 'Akossiwa', 'Kpogo', 'Labourer', 3500),
    SeedWorker(79, 'Elom', 'Klutse', 'Carpenter', 5500),
    SeedWorker(
      80,
      'Folly',
      'Lawson',
      'Electrician',
      1000,
      payType: PayType.hourly,
    ),
    SeedWorker(81, 'Afua', 'Mensah', 'Labourer', 3500),
    SeedWorker(82, 'Komi', 'Nyaku', 'Mason', 6000),
    SeedWorker(83, 'Abla', 'Ocloo', 'Labourer', 3500),
    SeedWorker(84, 'Yaovi', 'Quashie', 'Steel fixer', 5000),
    SeedWorker(85, 'Dzifa', 'Segbedji', 'Painter', 4500),
    SeedWorker(86, 'Atsu', 'Tchalla', 'Plumber', 6500),
    SeedWorker(87, 'Adjoa', 'Tossou', 'Labourer', 3500),
    SeedWorker(88, 'Kwami', 'Wilson', 'Mason', 6000, advance: 2000),
    SeedWorker(89, 'Eyram', 'Zinsou', 'Carpenter', 5500),
    SeedWorker(90, 'Senyo', 'Ahiadzro', 'Welder', 7500),
    SeedWorker(91, 'Amavi', 'Agbobli', 'Labourer', 3500),
    SeedWorker(92, 'Kodzo', 'Akue', 'Mason', 6000),
    SeedWorker(93, 'Mawuena', 'Apedo', 'Labourer', 3500),
    SeedWorker(94, 'Yayra', 'Avoke', 'Painter', 4500),
    SeedWorker(95, 'Komla', 'Dogbe', 'Steel fixer', 5000),
    SeedWorker(96, 'Sitsofe', 'Gavor', 'Labourer', 3500),
    SeedWorker(
      97,
      'Edinam',
      'Kodjovi',
      'Electrician',
      900,
      payType: PayType.hourly,
    ),
    SeedWorker(98, 'Nana', 'Togbe', 'Mason', 6000),
  ];

  static CrewWorker crewWorker(SeedWorker w) => CrewWorker(
    id: w.id,
    number: 'W-0${w.id + 100}',
    firstName: w.firstName,
    lastName: w.lastName,
    trade: w.trade,
    photoUrl: null,
    payType: w.payType,
    absentStreak: w.absentStreak,
  );

  /// Smaller crew for the second site.
  static const site2Workers = <SeedWorker>[
    SeedWorker(120, 'Kafui', 'Akoto', 'Mason', 6000),
    SeedWorker(121, 'Selassie', 'Bruce', 'Labourer', 3500),
    SeedWorker(122, 'Dodzi', 'Kpeglo', 'Carpenter', 5500),
    SeedWorker(123, 'Afi', 'Sedzro', 'Labourer', 3500),
    SeedWorker(124, 'Kekeli', 'Hodo', 'Welder', 7500),
    SeedWorker(125, 'Elikem', 'Tay', 'Painter', 4500),
  ];
}
