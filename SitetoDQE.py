

import os
import re
import time
import ast
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import flywheel
from flywheel.models import ContainerDeleteReason
from pytz import timezone as pytz_timezone
from fw_client import FWClient
from utils.tasks import create_task,get_pool_id_by_label,get_published_protocol_by_label  #local file

# Configuration
api_key_path = "/home/myounis/copysubjectscript/api_key.txt"
LOCAL_TIMEZONE = pytz_timezone("America/Chicago")  
BASE_URL = "https://wrc.flywheel.io:443/api"

source_group_ID = "abiduw"
certification_group_ID = "certification"
excluded_projects = {"Completed", "DQE", "Participant History", "Archive"}
destination_project_name = "DQE"

excluded_initials = {"FP", "FA", "OCT", "FAF", "RE", "LE", "OU", "CERT"}

# Retry in case Flywheel times out
def flywheel_modify_with_retry(modify_func, object_id, body, object_type="object", retries=5):
    for attempt in range(retries):
        try:
            return modify_func(object_id, body)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == retries - 1:
                print(f"ERROR: Could not modify {object_type} {object_id} after {retries} attempts.")
                raise
            wait = 10 ** attempt
            print(
                f"Flywheel connection/timeout error for {object_type} {object_id}: {e}\n"
                f"Retrying in {wait}s (attempt {attempt + 2}/{retries})"
            )
            time.sleep(wait)



#this is used for certifications, we extract the initial of the Tech
def extract_initials(filename):
    for token in re.split(r"[_\W]+", filename):
        if re.fullmatch(r"[A-Z]{2,3}", token) and token not in excluded_initials:
            return token
    return None

#for renaming sessions/subjects: This will help certification team figure out to which study the submission belongs
def extract_prefix(s):
    number, rest = s.split(" - ", 1)
    first_word = rest.split()[0]
    return f"{number} - {first_word}"


def acq_tags(acq):
    return acq["tags"] if acq["tags"] else []

#get the files submited
def site_submission_files(acq):
    """Files uploaded by the site (not yet processed by DQE)."""
    return [
        f for f in acq.files
        if (f.name.lower().endswith(".dcm.zip")
            or f.name.lower().endswith(".zip")
            or f.name.lower().endswith(".pdf"))
        and "dqe" not in f.name.lower()
    ]



def resolve_and_move(
    fw, fw_client, acq, session, project, subject,
    destination_project, subject_label, session_label,
    protocol_label, assignee, also_move=None, task_target="self",
):
    

    acq, destination_session = move_acq(fw, acq, destination_project, subject_label, session_label)

    if task_target == "self":
        create_task(fw_client, acq, destination_session, protocol_label, assignee)
    elif task_target == "companion":
        if also_move is not None:
            also_move, destination_session = move_acq(fw, also_move, destination_project, subject_label, session_label)
            create_task(fw_client, also_move, destination_session, protocol_label, assignee)
    else:
        raise ValueError(f"Unknown task_target: {task_target}")

    


# Moving acquisitions / sessions (rename-on-conflict)
def move_or_rename_acquisition(fw, acq, destination_session):
    """
    If an acquisition with the same label already exists in the destination_session, rename it first then move it
    """
    destination_acquisitions = list(destination_session.acquisitions())
    if any(a.label == acq.label for a in destination_acquisitions):
        print(f"Acquisition '{acq.label}' already exists in destination. Renaming in source project")
        timestamp = int(time.time() * 1000) % 1000
        new_label = f"{acq.label}_{timestamp}"
        flywheel_modify_with_retry(fw.modify_acquisition, acq.id, {"label": new_label}, object_type="acquisition")
        acq = fw.get_acquisition(acq.id)

    print(f"Moving acquisition '{acq.label}' to destination session '{destination_session.id}'")
    flywheel_modify_with_retry(
        fw.modify_acquisition, acq.id, {"session": destination_session.id}, object_type="acquisition")
    return acq


def move_acq(fw, acq, destination_project, subject_label, session_label):
    destination_subject = destination_project.subjects.find_one(f'label="{subject_label}"')
    destination_session = None
    if destination_subject:
        destination_session = destination_subject.sessions.find_first(f'label="{session_label}"')
        if not destination_session:
            print(f"Creating session '{session_label}' under subject '{subject_label}'")
            try:
                destination_session = destination_project.add_session(label=session_label, subject=destination_subject.id)
            except Exception as e:
                print(f"Error creating session '{session_label}': {e}")
        destination_session = destination_session.reload()
        acq = move_or_rename_acquisition(fw, acq, destination_session)
    return acq, destination_session



# Session-type handlers
def handle_patient_information_session(fw, group_ids, session, subject, subject_label):
    session = session.reload()
    destination_project2 = fw.lookup(f"{group_ids[0]}/Participant History")
    info = session.info

    viewer_key = None
    for key in ["ohifViewer"]:
        if key in info and "read" in info[key]:
            viewer_key = key
            break
    if not viewer_key:
        return

    reads = info["ohifViewer"]["read"]
    graders_info = list(reads.keys())
    if not graders_info or str(reads[graders_info[0]].get("readStatus")) != "Complete":
        print(f"skipping subject {subject_label} and timepoint {session.label} as patient info is not complete")
        return

    destination_subject2 = destination_project2.subjects.find_first(f'label="{subject_label}"')
    if destination_subject2 is None:
        try:
            destination_subject2 = destination_project2.subjects.find_one(f'label="{subject_label}"')
        except ValueError:
            destination_subject2 = destination_project2.add_subject({"label": subject_label})

    print(f"Moving session '{session.label}' to project '{destination_project2.label}'")
    destination_sessions = fw.get_subject_sessions(destination_subject2.id)
    destination_session = next((s for s in destination_sessions if s.label == session.label), None)

    if destination_subject2:
        print(f"found a destination subject in {destination_project2.label}")
        if not destination_session:
            flywheel_modify_with_retry(
                fw.modify_session, session.id, {"subject": destination_subject2.id}, object_type="session")
        else:
            print(f"Session '{session.label}' already exists in destination. Renaming in source project")
            timestamp = int(time.time() * 1000) % 1000
            new_session_label = f"{session.label}_{timestamp}"
            flywheel_modify_with_retry(
                fw.modify_session, session.id, {"label": new_session_label}, object_type="session")
            session = fw.get_session(session.id)
            flywheel_modify_with_retry(
                fw.modify_session, session.id, {"subject": destination_subject2.id}, object_type="session")
    else:
        print("couldnt find a destination subject in DQE:")
        flywheel_modify_with_retry(
            fw.modify_session, session.id, {"project": destination_project2.id}, object_type="session")

#this is for regular study image submissions
def handle_visit1_session(
    fw, fw_client, session, subject, project, subject_label, session_label,
    destination_project,
):
    all_acqs = list(session.acquisitions())
    if not any(len(a.files) > 0 for a in all_acqs):
        return  # no files anywhere in the session, skip

    found_oct = False

    for acq in session.acquisitions():
        if "oct" in acq.label.lower():
            assignee = ""#modify this
            protocol_label = ""#modify this
        else:
            assignee = ""#modify this
            protocol_label = ""#modify this

        if "oct" in acq.label.lower():

            all_acqs = list(session.acquisitions())
            oct_original = next((a for a in all_acqs if "oct" in a.label.lower() and "original" in a.label.lower()), None)
            non_oct_original = next((a for a in all_acqs if "oct" in a.label.lower() and "original" not in a.label.lower()), None)

            if "original" in acq.label.lower():
                found_oct = True
                resolve_and_move(
                    fw, fw_client, acq, session, project, subject,
                    destination_project, subject_label, session_label,
                    protocol_label, assignee,
                    also_move=non_oct_original, task_target="companion",
                )
            elif not found_oct and not oct_original:
                # lone OCT acquisition with no "original" counterpart
                resolve_and_move(
                    fw, fw_client, acq, session, project, subject,
                    destination_project, subject_label, session_label,
                    protocol_label, assignee,
                    also_move=None, task_target="self",
                )

        elif "fp" in acq.label.lower():
            resolve_and_move(
                fw, fw_client, acq, session, project, subject,
                destination_project, subject_label, session_label,
                protocol_label, assignee,
                also_move=None, task_target="self",
            )


def handle_uploader_acknowledgement_session(fw, session, project, session_label, destination_project3, group_ids2):
    new_subject_label = f"ABID - {extract_prefix(project.label)}"

    for acq in session.acquisitions():
        if len(acq.files) == 0:
            continue

        print(f'Found file in acquisition "{acq.label}"')
        print(f'Moving session "{session_label}" to project "{destination_project3.label}"  ')

        destination_subject = destination_project3.subjects.find_first(f'label="{new_subject_label}"')
        if not destination_subject:
            print(f"Creating subject '{new_subject_label}' in destination project")
            destination_subject = destination_project3.add_subject(label=new_subject_label)
            flywheel_modify_with_retry(
                fw.modify_session, session.id, {"subject": destination_subject.id}, object_type="session")
            return

        existing_session = next((s for s in destination_subject.sessions() if s.label == session.label), None)
        if not existing_session:
            flywheel_modify_with_retry(
                fw.modify_session, session.id, {"subject": destination_subject.id}, object_type="session")
            return

        print(f'Session "{session.label}" already exists.')
        for a in session.acquisitions():
            print(f'Moving acquisition "{a.label}"')
            move_or_rename_acquisition(fw, a, existing_session)
        return


def handle_certification_session(fw, session, subject_label, session_label, project, destination_project3):
    if "equipment" in str(subject_label).lower():
        new_subject_label = f"ABID - {extract_prefix(project.label)}"
        new_session_label = "Equipment"
    else:
        new_subject_label = f"ABID - {extract_prefix(project.label)}"
        new_session_label = subject_label

    destination_subject = destination_project3.subjects.find_first(f'label="{new_subject_label}"')
    if not destination_subject:
        print(f"Creating subject '{new_subject_label}' in destination project")
        destination_subject = destination_project3.add_subject(label=new_subject_label)

    for acquisition in session.acquisitions():
        if len(acquisition.files) == 0:
            continue

        destination_session = destination_subject.sessions.find_first(f'label="{new_session_label}"')
        if destination_session is None:
            print(f"Creating session {new_session_label}")
            destination_session = destination_subject.add_session(label=new_session_label)

        new_acquisition_label = None
        for file in acquisition.files:
            initial = extract_initials(file.name)
            if initial and initial not in acquisition.label:
                new_acquisition_label = f"{acquisition.label}_{initial}"
                flywheel_modify_with_retry(
                    fw.modify_acquisition, acquisition.id, {"label": new_acquisition_label}, object_type="acquisition")
                acquisition = fw.get_acquisition(acquisition.id)
                break

        move_or_rename_acquisition(fw, acquisition, destination_session)


def main():
    global api_key

    delete_reason = ContainerDeleteReason.CONTAINER_IS_EMPTY

    os.system("cls" if os.name == "nt" else "clear")
    with open(api_key_path, "r") as f:
        api_key = f.read()

    fw = flywheel.Client(api_key)
    fw_client = FWClient(api_key=api_key)

    group_ids = [source_group_ID]
    group_ids2 = [certification_group_ID]
    destination_project3 = fw.lookup(f"{group_ids2[0]}/Certification Review")

    source_projects = [
        p for p in fw.projects()
        if p.group == group_ids[0] and p.label not in excluded_projects and p.label[0].isdigit()
    ]
    source_projects = [p for p in source_projects if p.stats.number_of["files"] > 0]
    print("project_names:", [p.label for p in source_projects])

    if not source_projects:
        return

    group = fw.groups.find_first(f'_id="{group_ids[0]}"')
    if not group:
        print(f"Group {group_ids} not found")
        return

    for project in source_projects:
        if project.stats.number_of["files"] == 0:
            continue

        source_project = next((p for p in group.projects() if p.label == project.label), None)
        if not source_project:
            print(f"Source project '{project}' not found")
            continue

        destination_project = fw.lookup(f"{group_ids[0]}/{destination_project_name}")
        if not destination_project:
            print(f"Destination project '{destination_project_name}' not found")
            continue

        for session in source_project.sessions():
            subject = session.subject

            session_label = session.label
            subject_label = subject.label if subject else "default"

            destination_subject = destination_project.subjects.find_first(f'label="{subject_label}"')
            if not destination_subject:
                print(f"Creating subject '{subject_label}' in destination project")
                destination_project.add_subject(label=subject_label)
            if session_label == "Patient Information":
                handle_patient_information_session(fw, group_ids, session, subject, subject_label)

            elif "visit 1" in str(session_label).lower():
                handle_visit1_session(
                    fw, fw_client, session, subject, project, subject_label, session_label,
                    destination_project,
                )

            elif "uploader acknowledgement" in str(session_label).lower():
                handle_uploader_acknowledgement_session(
                    fw, session, project, session_label, destination_project3, group_ids2)

            elif "certification" in str(session_label).lower():
                handle_certification_session(
                    fw, session, subject_label, session_label, project, destination_project3)

            session = session.reload()
            if len(session.acquisitions()) == 0:
                print(f"The session at the source project is now empty, so it will be deleted: "
                        f"{session.label} and subject {subject_label}")
                fw.delete_session(session.id, delete_reason=delete_reason)


if __name__ == "__main__":
    main()
