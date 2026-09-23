from aedes_bi.extract import Extract
from aedes_bi.load import Load
from aedes_bi.transform import Transform


def main() -> None:
    extractor = Extract()
    boundary = extractor.download_recife_boundary()
    transformer = Transform(boundary)
    loader = Load()

    edls = transformer.transform_edls(extractor.read_edls())
    locations = transformer.transform_locations(extractor.read_ovt_locations())
    observations, cycles = transformer.transform_observations(extractor.read_ovt_observations())
    coordinate_audit, date_audit = transformer.audits()
    loader.load_sqlite(edls, locations, observations, cycles, coordinate_audit, date_audit)
    print(f"Carga concluída: {len(edls)} EDLs, {len(locations)} ovitrampas e {len(observations)} observações.")


if __name__ == "__main__":
    main()
