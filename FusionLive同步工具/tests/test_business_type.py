import csv
from pathlib import Path
import tempfile
import unittest

from business_type import classify, classify_csv, classify_pdf, load_rules, parse_number, pdf_unit, unit_names


BASE = '30201_50150_LT_HE-H4-262-'


class BusinessTypeTests(unittest.TestCase):
    def test_combines_discipline_type_and_serial(self):
        self.assertEqual(parse_number(BASE+'13-20-001')['business_type'], '管道布置平面及立面图')
        self.assertEqual(parse_number(BASE+'00-30-499')['business_type'], '工艺管道及仪表流程图')
        self.assertEqual(parse_number(BASE+'00-30-500')['business_type'], '工艺管线表')
        self.assertEqual(parse_number(BASE+'00-30-550')['status'], 'discipline_only')

    def test_range_boundaries_and_gaps_are_preserved(self):
        self.assertEqual(parse_number(BASE+'13-65-079')['business_type'], '管道支架材料统计表')
        self.assertEqual(parse_number(BASE+'13-65-080')['status'], 'discipline_only')
        self.assertEqual(parse_number(BASE+'13-65-081')['business_type'], '管道支架清单')
        self.assertEqual(parse_number(BASE+'16-60-100')['business_type'], '电缆桥架布置图')

    def test_prefix_is_business_nature_even_when_drawing_code_differs(self):
        result = classify(filename='RPT-'+BASE+'13-20-001__rev_2__24415506.pdf')
        self.assertEqual(result['business_type'], '报告')
        self.assertEqual(result['discipline'], '管道')
        self.assertEqual(result['serial'], '001')

    def test_legacy_number_only_recognizes_explicit_prefix(self):
        self.assertEqual(parse_number('SP-30201_50150_LT_HE-H1-32-00-306E')['business_type'], '技术规格书')
        self.assertEqual(parse_number('30201_50150_LT_HE-H1-32-00-306E')['status'], 'unrecognized')

    def test_revision_and_extension_do_not_change_business(self):
        results = [classify(filename=BASE+'13-20-001'+suffix)['business_type']
                   for suffix in ('.pdf', '.zip', '__rev_A__123__abc.xlsx')]
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(classify(filename='PIPING LAYOUT.pdf')['status'], 'unrecognized')

    def test_actual_filename_precedes_package_reference(self):
        result = classify(filename=BASE+'16-60-100.pdf', reference='ENG_Package_GCC1')
        self.assertEqual(result['business_type'], '电缆桥架布置图')
        self.assertEqual(result['source'], '文件名')

    def test_attachment_inheritance_has_explicit_status(self):
        result = classify(filename='ADNOCGP-WTRAN-035823.pdf', reference='PP-'+BASE+'50-00-002')
        self.assertEqual(result['business_type'], '项目程序文件')
        self.assertEqual(result['status'], 'inherited')
        self.assertIn('尚未核验附件内容', result['reason'])

    def test_multiple_parent_types_are_not_silently_collapsed(self):
        result = classify(filename='shared.zip', parent_references=(BASE+'13-20-001', BASE+'16-60-100'))
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(parse_number(BASE+'13-20-001;'+BASE+'16-60-100')['status'], 'conflict')

    def test_directory_names_are_not_evidence(self):
        self.assertEqual(classify(path='C:/'+BASE+'13-20-001/vendor.zip')['status'], 'unrecognized')
        self.assertEqual(classify(path='C:/downloads/'+BASE+'13-20-001.pdf')['status'], 'recognized')

    def test_isometric_directory_and_ambiguous_rules_remain_unresolved(self):
        self.assertEqual(parse_number(BASE+'13-61-001')['status'], 'discipline_only')
        self.assertEqual(parse_number(BASE+'51-00-001')['status'], 'unrecognized')
        self.assertEqual(parse_number(BASE+'17-50-101')['status'], 'discipline_only')

    def test_construction_iso_numbers(self):
        self.assertEqual(classify(filename='5-521-52-0141-HCX-001-01.pdf')['business_type'], '地上新建管道单线图')
        self.assertEqual(classify(filename='5-521-45-0141-HCX-001-01.pdf')['discipline'], '土建')

    def test_pcf_export_suffix_preserves_construction_number(self):
        result = classify(filename='2-272-52-0001-AGX-WAB-pcf.pcf')
        self.assertEqual(result['business_type'], '地上新建管道单线图')
        self.assertEqual(result['status'], 'recognized')
        self.assertEqual((result['plant'], result['unit']), ('H2', '272'))
        self.assertEqual(result['matched_number'], '2-272-52-0001-AGX-WAB')
        self.assertIn('PCF-EXPORT-01', result['basis'])
        self.assertEqual(classify(filename='2-272-52-0001-AGX-WAB-PCF.PCF')['status'], 'recognized')
        self.assertNotIn('PCF-EXPORT-01', classify(filename='2-272-52-0001-AGX-WAB.pcf')['basis'])
        for suffix in ('-extra.pcf', '-pcf.pdf', '-pcf.pcfX'):
            self.assertEqual(classify(filename='2-272-52-0001-AGX-WAB'+suffix)['status'], 'unrecognized')

    def test_index_numbering_with_alphabetic_cwa_and_separate_sheet(self):
        for name in ('4-213-52-0001-CD-PAD.pdf', '4-213-52-0019-FL-PAC_0.pdf',
                     '4-213-52-0001-CD-PAD-01__rev_1__123.pdf'):
            with self.subTest(name=name):
                self.assertEqual(classify_pdf(name)[0], '单线图')
                self.assertEqual(pdf_unit(name, 'fallback'), 'H4-213')
                self.assertIn('13-61-021', classify_pdf(name)[1])

    def test_existing_unit_43_is_preserved_and_has_source(self):
        name = '1-43-52-1167-FLC-AAE_0.pdf'
        result = classify(filename=name)
        self.assertEqual(result['status'], 'recognized')
        self.assertEqual(result['unit'], '43')
        self.assertEqual(classify_pdf(name)[0], '单线图')
        self.assertEqual(pdf_unit(name, 'fallback'), 'H1-43')
        self.assertIn('150', result['basis'])
        self.assertEqual(unit_names('H1-43')['english'], 'Thammama B/C NGL Recovery Train 4')

    def test_invalid_iso_variants_do_not_match_a_valid_prefix(self):
        for name in ('1-99-52-1167-FLC-AAE.pdf', '4-43-52-1167-FLC-AAE.pdf',
                     '4-213-52-0001-CD-PAI.pdf', '4-213-52-0001-CD-PAQ.pdf',
                     '4-213-52-0001-CD-ZAA.pdf', '4-213-52-0001-CD-AZA.pdf',
                     '4-213-52-0001-CD-PAD-1.pdf', '4-213-52-0001-CD-PAD-001.pdf',
                     '4-213-52-0001-CD-PAD-01X.pdf', '4-213-52-0001-CD-PAD-extra.pdf',
                     '5-521-52-0141-HCX-001.pdf'):
            with self.subTest(name=name):
                self.assertEqual(classify_pdf(name)[0], '待分类PDF')
        self.assertEqual(classify_pdf('4-213-45-0001-CD-PAD.pdf')[0], '其他PDF')
        self.assertEqual(classify_pdf('4-213-52-0001-CD-PAD/unrelated.pdf')[0], '待分类PDF')

    def test_rules_have_no_contradictory_intervals(self):
        rules = load_rules()['document_types']
        for i, a in enumerate(rules):
            self.assertNotIn('?', a['label'])
            self.assertIn('附件2', a['basis'])
            self.assertLessEqual(a['start'], a['end'])
            for b in rules[i+1:]:
                if (a['discipline'], a['type']) == (b['discipline'], b['type']):
                    self.assertFalse(max(a['start'], b['start']) <= min(a['end'], b['end']))

    def test_csv_retains_rows_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp)/'in.csv', Path(temp)/'out.csv'
            with source.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['文件名', '文档编号', '本地路径'])
                writer.writerow([BASE+'13-20-001.pdf', 'package', 'downloads/a.pdf'])
                writer.writerow(['notice.pdf', 'package', 'downloads/notice.pdf'])
            before = source.read_bytes()
            counts = classify_csv(source, output)
            self.assertEqual(counts, {'recognized': 1, 'unrecognized': 1})
            self.assertEqual(source.read_bytes(), before)
            with output.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['业务类型'], '管道布置平面及立面图')
            with self.assertRaises(ValueError):
                classify_csv(source, source)
            with self.assertRaises(FileExistsError):
                classify_csv(source, output)


if __name__ == '__main__':
    unittest.main()
